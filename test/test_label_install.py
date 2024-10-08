from six import StringIO
import pytest
from neo4j.exceptions import DatabaseError
from neomodel import (
    config, StructuredNode, StringProperty, install_all_labels, install_labels,
    UniqueIdProperty)
from neomodel.core import db


config.AUTO_INSTALL_LABELS = False


class NoConstraintsSetup(StructuredNode):
    name = StringProperty(unique_index=True)


class AbstractNode(StructuredNode):
    __abstract_node__ = True
    name = StringProperty(unique_index=True)


class SomeNotUniqueNode(StructuredNode):
    id_ = UniqueIdProperty(db_property='id')


config.AUTO_INSTALL_LABELS = True


def test_labels_were_not_installed():
    bob = NoConstraintsSetup(name='bob').save()
    bob2 = NoConstraintsSetup(name='bob').save()
    bob3 = NoConstraintsSetup(name='bob').save()
    assert bob.id != bob3.id

    for n in NoConstraintsSetup.nodes.all():
        n.delete()


def test_install_all():
    install_labels(AbstractNode)
    # run install all labels
    install_all_labels()
    # remove constraint for above test
    db.cypher_query("DROP CONSTRAINT on (n:NoConstraintsSetup) ASSERT n.name IS UNIQUE")


def test_install_label_twice():
    install_labels(AbstractNode)
    install_labels(AbstractNode)


def test_install_labels_db_property():
    stdout = StringIO()
    install_labels(SomeNotUniqueNode, quiet=False, stdout=stdout)
    assert 'id' in stdout.getvalue()
    # make sure that the id_ constraint doesn't exist
    with pytest.raises(DatabaseError) as exc_info:
        db.cypher_query(
            'DROP CONSTRAINT on (n:SomeNotUniqueNode) ASSERT n.id_ IS UNIQUE')
    assert 'No such constraint' in exc_info.exconly()
    # make sure the id constraint exists and can be removed
    db.cypher_query('DROP CONSTRAINT on (n:SomeNotUniqueNode) ASSERT n.id IS UNIQUE')
