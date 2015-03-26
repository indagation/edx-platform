""" Code to allow module store to interface with courseware index """
from __future__ import absolute_import

from datetime import timedelta
import logging

from django.utils.translation import ugettext as _
from opaque_keys.edx.locator import CourseLocator
from search.search_engine_base import SearchEngine
from eventtracking import tracker

from . import ModuleStoreEnum
from .exceptions import ItemNotFoundError


# Use default index and document names for now
INDEX_NAME = "courseware_index"
DOCUMENT_TYPE = "courseware_content"
REINDEX_AGE = timedelta(0, 60)

log = logging.getLogger('edx.modulestore')


class SearchIndexingError(Exception):
    """ Indicates some error(s) occured during indexing """

    def __init__(self, message, error_list):
        super(SearchIndexingError, self).__init__(message)
        self.error_list = error_list


class CoursewareSearchIndexer(object):
    """
    Class to perform indexing for courseware search from different modulestores
    """

    @staticmethod
    def index_course(modulestore, course_key, raise_on_error=False, triggered_at=None):
        """
        Process course for indexing
        """
        error_list = []
        indexed_count = 0
        searcher = SearchEngine.get_search_engine(INDEX_NAME)
        if not searcher:
            return

        location_info = {
            "course": unicode(course_key),
        }

        indexed_items = set()

        course = modulestore.get_course(course_key, depth=None, revision=ModuleStoreEnum.RevisionOption.published_only)

        def index_item(item, current_start_date, skip_index=False):
            """ add this item to the search index """
            is_indexable = hasattr(item, "index_dictionary")
            # if it's not indexable and it does not have children, then ignore
            if not is_indexable and not item.has_children:
                return

            item_id = unicode(item.scope_ids.usage_id)
            indexed_items.add(item_id)

            # if it has a defined start, then apply it and to it's children
            if item.start and (not current_start_date or item.start > current_start_date):
                current_start_date = item.start

            if item.has_children:
                skip_child_index = skip_index or \
                    (triggered_at is not None and (triggered_at - item.subtree_edited_on) > REINDEX_AGE)
                for child_item in item.get_children():
                    index_item(child_item, current_start_date, skip_index=skip_child_index)

            if skip_index:
                return

            item_index = {}
            item_index_dictionary = item.index_dictionary() if is_indexable else None

            # if it has something to add to the index, then add it
            if item_index_dictionary:
                try:
                    item_index.update(location_info)
                    item_index.update(item_index_dictionary)
                    item_index['id'] = item_id
                    if current_start_date:
                        item_index['start_date'] = current_start_date

                    searcher.index(DOCUMENT_TYPE, item_index)
                except Exception as err:  # pylint: disable=broad-except
                    # broad exception so that index operation does not fail on one item of many
                    log.warning('Could not index item: %s - %r', item.location, err)
                    error_list.append(_('Could not index item: {}').format(item.location))

        def remove_deleted_items():
            """
            remove any item that is present in the search index that is not present in updated list of indexed items
            as we find items we can shorten the set of items to keep
            """
            response = searcher.search(
                doc_type=DOCUMENT_TYPE,
                field_dictionary={"course": unicode(course_key)},
                exclude_ids=indexed_items
            )
            result_ids = [result["data"]["id"] for result in response["results"]]
            for result_id in result_ids:
                searcher.remove(DOCUMENT_TYPE, result_id)

        try:
            for item in course.get_children():
                index_item(item, course.start)
            remove_deleted_items()
        except Exception as err:  # pylint: disable=broad-except
            # broad exception so that index operation does not prevent the rest of the application from working
            log.exception(
                "Indexing error encountered, courseware index may be out of date %s - %r",
                course_key,
                err
            )
            error_list.append(_('General indexing error occurred'))

        if raise_on_error and error_list:
            raise SearchIndexingError(_('Error(s) present during indexing'), error_list)

        return indexed_count

    @classmethod
    def do_course_reindex(cls, modulestore, course_key, triggered_at=None):
        """
        (Re)index all content within the given course
        """
        indexed_count = cls.index_course(modulestore, course_key, raise_on_error=True, triggered_at=triggered_at)
        cls._track_index_request('edx.course.index.reindexed', indexed_count)
        return indexed_count

    @staticmethod
    def _track_index_request(event_name, indexed_count, location=None):
        """Track content index requests.

        Arguments:
            location (str): The ID of content to be indexed.
            event_name (str):  Name of the event to be logged.
        Returns:
            None

        """
        data = {
            "indexed_count": indexed_count,
            'category': 'courseware_index',
        }

        if location:
            data['location_id'] = location

        tracker.emit(
            event_name,
            data
        )
