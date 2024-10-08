import functools
import inspect
import sys
from importlib import import_module
from typing import List
from uuid import UUID

from .core import db
from .exceptions import NotConnected, RelationshipClassRedefined
from .match import EITHER, INCOMING, OUTGOING, NodeSet, Traversal, \
    _rel_helper, _rel_merge_helper
from .relationship import StructuredRel
from .util import _get_node_properties, deprecated


# basestring python 3.x fallback
try:
    basestring
except NameError:
    basestring = str


# check source node is saved and not deleted
def check_source(fn):
    fn_name = fn.func_name if hasattr(fn, 'func_name') else fn.__name__

    @functools.wraps(fn)
    def checker(self, *args, **kwargs):
        self.source._pre_action_check(self.name + '.' + fn_name)
        return fn(self, *args, **kwargs)
    return checker

# checks if obj is a direct subclass, 1 level
def is_direct_subclass(obj, classinfo):
    for base in obj.__bases__:
        if base == classinfo:
            return True
    return False

class RelationshipManager(object):
    """
    Base class for all relationships managed through neomodel.

    I.e the 'friends' object in  `user.friends.all()`
    """
    def __init__(self, source, key, definition):
        self.source = source
        self.source_class = source.__class__
        self.name = key
        self.definition = definition

    def __str__(self):
        direction = 'either'
        if self.definition['direction'] == OUTGOING:
            direction = 'a outgoing'
        elif self.definition['direction'] == INCOMING:
            direction = 'a incoming'

        return "{0} in {1} direction of type {2} on node ({3}) of class '{4}'".format(
            self.description, direction,
            self.definition['relation_type'], self.source.id, self.source_class.__name__)

    def _check_node(self, obj):
        """check for valid node i.e correct class and is saved"""
        if not issubclass(type(obj), self.definition['node_class']):
            raise ValueError("Expected node of class " + self.definition['node_class'].__name__)
        if not hasattr(obj, 'id'):
            raise ValueError("Can't perform operation on unsaved node " + repr(obj))

    def connect_helper(self, query: str, properties=None, node=None, nodes=None):
        """
        Function to support both connect() and bulk_connect() functions.

        The beginning of the query - either UNWIND or MATCH - is set in the function itself
        and sent in the query parameter. The rest of the query in both connect() and
        bulk_connect() is very similar so it is built and executed here.

        """
        if not self.definition['model'] and properties:
            raise NotImplementedError(
                "Relationship properties without using a relationship model "
                "is no longer supported."
            )

        params = {}
        rel_model = self.definition['model']
        rp = None  # rel_properties

        if rel_model:
            rp = {}
            # need to generate defaults etc to create fake instance
            tmp = rel_model(**properties) if properties else rel_model()
            # build params and place holders to pass to rel_helper
            for p, v in rel_model.deflate(tmp.__properties__).items():
                if v is not None:
                    rp[p] = '$' + p
                else:
                    rp[p] = None
                params[p] = v

            if hasattr(tmp, 'pre_save'):
                tmp.pre_save()

        new_rel = _rel_merge_helper(lhs='us', rhs='them', ident='r', relation_properties=rp, **self.definition)
        q = query + new_rel

        if node:
            params['them'] = node.id
        else:
            params['them'] = "uuid"
            params['uuids'] = nodes

        if not rel_model:
            self.source.cypher(q, params)
            return True

        rel_ = self.source.cypher(q + " RETURN r", params)[0][0][0]
        rel_instance = self._set_start_end_cls(rel_model.inflate(rel_), node)

        if hasattr(rel_instance, 'post_save'):
            rel_instance.post_save()

        return rel_instance

    @check_source
    def connect(self, node, properties=None):
        """
        Connect a node

        :param node: a node instance which will be connected to the source node
        :param properties: for the new relationship
        :type: dict
        :return:
        """
        self._check_node(node)
        source_label = self.source.__class__.__name__
        dest_label = node.__class__.__name__
        q = f"MATCH (them:{dest_label}), (us:{source_label}) WHERE id(them)=$them and id(us)=$self " \
            "MERGE"
        return self.connect_helper(q, properties, node)

    def bulk_disconnect(self, nodes: List[UUID]):
        """
        Disconnect list of nodes from the source node

        :param nodes: a list of node UUIDs which will be connected to the source node
        """
        if nodes:
            nodes = [str(uuid) for uuid in nodes]
            rel = _rel_helper(lhs='a', rhs='b', ident='r', **self.definition)
            q = "MATCH (a), (b) WHERE id(a)=$self and b.uuid in $them " \
                "MATCH " + rel + " DELETE r"
            print(q, nodes)
            self.source.cypher(q, {'them': nodes})

    @check_source
    def bulk_connect(self, nodes: List[UUID], label, properties=None):
        """
        Connect a list of nodes to the source node.

        :param nodes: a list of node UUIDs which will be connected to the source node
        :param label: Label for nodes in list
        :param properties: for the new relationship
        :type: dict
        :return:

        Unlike `connect()`, this function receives a list of node UUIDs,
        not a node instance. Using UNWIND, the query connects all nodes in
        the list to the source node.
        """
        if nodes:
            nodes = [str(uuid) for uuid in nodes]
            source_label = self.source.__class__.__name__
            q = f"UNWIND $uuids as uuid MATCH (them:{label}), (us:{source_label}) WHERE them.uuid=uuid " \
                "and id(us)=$self MERGE"
            return self.connect_helper(q, properties=properties, nodes=nodes)


    @check_source
    def replace(self, node, properties=None):
        """
        Disconnect all existing nodes and connect the supplied node

        :param node:
        :param properties: for the new relationship
        :type: dict
        :return:
        """
        self.disconnect_all()
        self.connect(node, properties)

    @check_source
    def relationship(self, node):
        """
        Retrieve the relationship object for this first relationship between self and node.

        :param node:
        :return: StructuredRel
        """
        self._check_node(node)
        my_rel = _rel_helper(lhs='us', rhs='them', ident='r', **self.definition)
        q = "MATCH " + my_rel + " WHERE id(them)=$them and id(us)=$self RETURN r LIMIT 1"
        rels = self.source.cypher(q, {'them': node.id})[0]
        if not rels:
            return

        rel_model = self.definition.get('model') or StructuredRel

        return self._set_start_end_cls(rel_model.inflate(rels[0][0]), node)

    @check_source
    def all_relationships(self, node):
        """
        Retrieve all relationship objects between self and node.

        :param node:
        :return: [StructuredRel]
        """
        self._check_node(node)

        my_rel = _rel_helper(lhs='us', rhs='them', ident='r', **self.definition)
        q = "MATCH " + my_rel + " WHERE id(them)=$them and id(us)=$self RETURN r "
        rels = self.source.cypher(q, {'them': node.id})[0]
        if not rels:
            return []

        rel_model = self.definition.get('model') or StructuredRel
        return [self._set_start_end_cls(rel_model.inflate(rel[0]), node) for rel in rels]

    def _set_start_end_cls(self, rel_instance, obj):
        if self.definition['direction'] == INCOMING:
            rel_instance._start_node_class = obj.__class__
            rel_instance._end_node_class = self.source_class
        else:
            rel_instance._start_node_class = self.source_class
            rel_instance._end_node_class = obj.__class__
        return rel_instance

    @check_source
    def reconnect(self, old_node, new_node):
        """
        Disconnect old_node and connect new_node copying over any properties on the original relationship.

        Useful for preventing cardinality violations

        :param old_node:
        :param new_node:
        :return: None
        """

        self._check_node(old_node)
        self._check_node(new_node)
        if old_node.id == new_node.id:
            return
        old_rel = _rel_helper(lhs='us', rhs='old', ident='r', **self.definition)

        # get list of properties on the existing rel
        result, meta = self.source.cypher(
            "MATCH (us), (old) WHERE id(us)=$self and id(old)=$old "
            "MATCH " + old_rel + " RETURN r", {'old': old_node.id})
        if result:
            node_properties = _get_node_properties(result[0][0])
            existing_properties = node_properties.keys()
        else:
            raise NotConnected('reconnect', self.source, old_node)

        # remove old relationship and create new one
        new_rel = _rel_merge_helper(lhs='us', rhs='new', ident='r2', **self.definition)
        q = "MATCH (us), (old), (new) " \
            "WHERE id(us)=$self and id(old)=$old and id(new)=$new " \
            "MATCH " + old_rel
        q += " MERGE" + new_rel

        # copy over properties if we have
        for p in existing_properties:
            q += " SET r2.{0} = r.{1}".format(p, p)
        q += " WITH r DELETE r"

        self.source.cypher(q, {'old': old_node.id, 'new': new_node.id})

    @check_source
    def disconnect(self, node):
        """
        Disconnect a node

        :param node:
        :return:
        """
        rel = _rel_helper(lhs='a', rhs='b', ident='r', **self.definition)
        q = "MATCH (a), (b) WHERE id(a)=$self and id(b)=$them " \
            "MATCH " + rel + " DELETE r"
        self.source.cypher(q, {'them': node.id})

    @check_source
    def disconnect_all(self):
        """
        Disconnect all nodes

        :return:
        """
        rhs = 'b:' + self.definition['node_class'].__label__
        rel = _rel_helper(lhs='a', rhs=rhs, ident='r', **self.definition)
        q = 'MATCH (a) WHERE id(a)=$self MATCH ' + rel + ' DELETE r'
        self.source.cypher(q)

    @check_source
    def _new_traversal(self):
        return Traversal(self.source, self.name, self.definition)

    # The methods below simply proxy the match engine.
    def get(self, **kwargs):
        """
        Retrieve a related node with the matching node properties.

        :param kwargs: same syntax as `NodeSet.filter()`
        :return: node
        """
        return NodeSet(self._new_traversal()).get(**kwargs)

    def get_or_none(self, **kwargs):
        """
        Retrieve a related node with the matching node properties or return None.

        :param kwargs: same syntax as `NodeSet.filter()`
        :return: node
        """
        return NodeSet(self._new_traversal()).get_or_none(**kwargs)

    @deprecated("search() is now deprecated please use filter() and exclude()")
    def search(self, **kwargs):
        """
        Retrieve related nodes matching the provided properties.

        :param kwargs: same syntax as `NodeSet.filter()`
        :return: NodeSet
        """
        return self.filter(**kwargs).all()

    def filter(self, *args, **kwargs):
        """
        Retrieve related nodes matching the provided properties.

        :param args: a Q object
        :param kwargs: same syntax as `NodeSet.filter()`
        :return: NodeSet
        """
        return NodeSet(self._new_traversal()).filter(*args, **kwargs)

    def order_by(self, *props):
        """
        Order related nodes by specified properties

        :param props:
        :return: NodeSet
        """
        return NodeSet(self._new_traversal()).order_by(*props)

    def exclude(self, *args, **kwargs):
        """
        Exclude nodes that match the provided properties.

        :param args: a Q object
        :param kwargs: same syntax as `NodeSet.filter()`
        :return: NodeSet
        """
        return NodeSet(self._new_traversal()).exclude(*args, **kwargs)

    def is_connected(self, node):
        """
        Check if a node is connected with this relationship type
        :param node:
        :return: bool
        """
        return self._new_traversal().__contains__(node)

    def single(self):
        """
        Get a single related node or none.

        :return: StructuredNode
        """
        try:
            return self[0]
        except IndexError:
            pass

    def match(self, **kwargs):
        """
        Return set of nodes who's relationship properties match supplied args

        :param kwargs: same syntax as `NodeSet.filter()`
        :return: NodeSet
        """
        return self._new_traversal().match(**kwargs)

    def all(self):
        """
        Return all related nodes.

        :return: list
        """
        return self._new_traversal().all()

    def values(self, *args):
        """
        Return only specified fields for each node.

        :param args: string field names
        :return: NodeSet
        """
        return NodeSet(self._new_traversal()).values(*args)

    def __iter__(self):
        return self._new_traversal().__iter__()

    def __len__(self):
        return self._new_traversal().__len__()

    def __bool__(self):
        return self._new_traversal().__bool__()

    def __nonzero__(self):
        return self._new_traversal().__nonzero__()

    def __contains__(self, obj):
        return self._new_traversal().__contains__(obj)

    def __getitem__(self, key):
        return self._new_traversal().__getitem__(key)


class RelationshipDefinition(object):
    def __init__(self, relation_type, cls_name, direction, manager=RelationshipManager, model=None):
        current_frame = inspect.currentframe()

        def enumerate_traceback(initial_frame):
            depth, frame = 0, initial_frame
            while frame is not None:
                yield depth, frame
                frame = frame.f_back
                depth += 1

        frame_number = 4
        for i, frame in enumerate_traceback(current_frame):
            if cls_name in frame.f_globals:
                frame_number = i
                break
        self.module_name = sys._getframe(frame_number).f_globals['__name__']
        if '__file__' in sys._getframe(frame_number).f_globals:
            self.module_file = sys._getframe(frame_number).f_globals['__file__']
        self._raw_class = cls_name
        self.manager = manager
        self.definition = {}
        self.definition['relation_type'] = relation_type
        self.definition['direction'] = direction
        self.definition['model'] = model

        if model is not None:
            # Relationships are easier to instantiate because (at the moment), they cannot have multiple labels. So, a
            # relationship's type determines the class that should be instantiated uniquely. Here however, we still use
            # a `frozenset([relation_type])` to preserve the mapping type.
            label_set = frozenset([relation_type])
            try:
                # If the relationship mapping exists then it is attempted to be redefined so that it applies to the same
                # label. In this case, it has to be ensured that the class that is overriding the relationship is a
                # descendant of the already existing class
                model_from_registry = db._NODE_CLASS_REGISTRY[label_set]
                if not issubclass(model, model_from_registry):
                    is_parent = issubclass(model_from_registry, model)
                    if is_direct_subclass(model, StructuredRel) and not is_parent:
                        raise RelationshipClassRedefined(relation_type, db._NODE_CLASS_REGISTRY, model)
                else:
                    db._NODE_CLASS_REGISTRY[label_set] = model
            except KeyError:
                # If the mapping does not exist then it is simply created.
                db._NODE_CLASS_REGISTRY[label_set] = model

    def _lookup_node_class(self):
        if not isinstance(self._raw_class, basestring):
            self.definition['node_class'] = self._raw_class
        else:
            name = self._raw_class
            if name.find('.') == -1:
                module = self.module_name
            else:
                module, _, name = name.rpartition('.')

            if module not in sys.modules:
                # yet another hack to get around python semantics
                # __name__ is the namespace of the parent module for __init__.py files,
                # and the namespace of the current module for other .py files,
                # therefore there's a need to define the namespace differently for
                # these two cases in order for . in relative imports to work correctly
                # (i.e. to mean the same thing for both cases).
                # For example in the comments below, namespace == myapp, always
                if not hasattr(self, 'module_file'):
                    raise ImportError("Couldn't lookup '{0}'".format(name))

                if '__init__.py' in self.module_file:
                    # e.g. myapp/__init__.py -[__name__]-> myapp
                    namespace = self.module_name
                else:
                    # e.g. myapp/models.py -[__name__]-> myapp.models
                    namespace = self.module_name.rpartition('.')[0]

                # load a module from a namespace (e.g. models from myapp)
                if module:
                    module = import_module(module, namespace).__name__
                # load the namespace itself (e.g. myapp)
                # (otherwise it would look like import . from myapp)
                else:
                    module = import_module(namespace).__name__
            self.definition['node_class'] = getattr(sys.modules[module], name)

    def build_manager(self, source, name):
        self._lookup_node_class()
        return self.manager(source, name, self.definition)


class ZeroOrMore(RelationshipManager):
    """
    A relationship of zero or more nodes (the default)
    """
    description = "zero or more relationships"


def _relate(cls_name, direction, rel_type, cardinality=None, model=None):
    if not isinstance(cls_name, (basestring, object)):
        raise ValueError('Expected class name or class got ' + repr(cls_name))

    if model and not issubclass(model, (StructuredRel,)):
        raise ValueError('model must be a StructuredRel')
    return RelationshipDefinition(rel_type, cls_name, direction, cardinality, model)


def RelationshipTo(cls_name, rel_type, cardinality=ZeroOrMore, model=None):
    return _relate(cls_name, OUTGOING, rel_type, cardinality, model)


def RelationshipFrom(cls_name, rel_type, cardinality=ZeroOrMore, model=None):
    return _relate(cls_name, INCOMING, rel_type, cardinality, model)


def Relationship(cls_name, rel_type, cardinality=ZeroOrMore, model=None):
    return _relate(cls_name, EITHER, rel_type, cardinality, model)
