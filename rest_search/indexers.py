# -*- coding: utf-8 -*-

from django.db.models.signals import post_delete, post_save
from elasticsearch.helpers import scan

from rest_search import get_elasticsearch


_REGISTERED_CLASSES = []


def bulk_iterate(queryset, block_size=1000):
    """
    Iterates over a huge queryset in chunks of 1000 items.
    """
    total = queryset.count()
    queryset = queryset.order_by('pk')
    for start in range(0, total, block_size):
        for item in queryset[start:start + block_size]:
            yield item


class Indexer(object):
    mappings = None
    private_properties = []

    def __init__(self):
        self.doc_type = self.serializer_class.Meta.model.__name__

    def iterate_items(self, remove=True):
        """
        Generates items to perform a full resync of the index.
        """
        es = get_elasticsearch(self)
        index = es._index
        queryset = self.get_queryset()

        # index current items
        ids = set()
        for item in bulk_iterate(queryset):
            ids.add(item.pk)
            yield self.__add_item(item, index=index)

        # remove obsolete items
        if remove:
            for i in scan(es, index=index, doc_type=self.doc_type, fields=[]):
                pk = int(i['_id'])
                if pk not in ids:
                    yield self.__remove_item(pk, index=index)

    def partial_items(self, pks):
        """
        Generates items to perform a partial update of the index.

        pks is a list of primary keys of items which have changed
        or been deleted.
        """
        es = get_elasticsearch(self)
        index = es._index
        queryset = self.get_queryset().filter(pk__in=pks)

        # index current items
        removed = set(pks)
        for item in bulk_iterate(queryset):
            removed.discard(item.pk)
            yield self.__add_item(item, index=index)

        # remove obsolete items
        for pk in removed:
            yield self.__remove_item(pk, index=index)

    def map_results(self, results):
        """
        Removes ES-specific fields from results.
        """
        def map_result_item(x):
            item = x['_source']
            for key in self.private_properties:
                item.pop(key, None)
            return item
        return map(map_result_item, results)

    def scan(self, **kwargs):
        es = get_elasticsearch(self)
        return scan(es, index=es._index, doc_type=self.doc_type, **kwargs)

    def search(self, **kwargs):
        es = get_elasticsearch(self)
        return es.search(index=es._index, doc_type=self.doc_type, **kwargs)

    def __add_item(self, item, index):
        return {
            '_index': index,
            '_type': self.doc_type,
            '_id': item.pk,
            '_source': self.serializer_class(item).data
        }

    def __remove_item(self, pk, index):
        return {
            '_index': index,
            '_type': self.doc_type,
            '_id': pk,
            '_op_type': 'delete',
        }


def _get_registered():
    """
    Returns instances of all registered indexers.
    """
    return [cls() for cls in _REGISTERED_CLASSES]


def _instance_changed(sender, instance, **kwargs):
    """
    Queues an update to the ElasticSearch index.
    """
    from rest_search import queue_add
    doc_type = instance.__class__.__name__
    queue_add({
        doc_type: [instance.pk],
    })


def register(indexer_class):
    """
    Registers an indexer class.
    """
    if indexer_class not in _REGISTERED_CLASSES:
        _REGISTERED_CLASSES.append(indexer_class)

        # register signal handlers
        model = indexer_class.serializer_class.Meta.model
        post_delete.connect(_instance_changed, sender=model)
        post_save.connect(_instance_changed, sender=model)
