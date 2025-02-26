from plex_database.matcher import Default as Matcher
from plex_database.models import *
from plex_metadata.guid import Guid

from datetime import datetime
from peewee import JOIN_LEFT_OUTER, DateTimeField, FieldProxy
from stash.algorithms.core.prime_context import PrimeContext
import logging
import peewee

log = logging.getLogger(__name__)

# Optional tzlocal/pytz import
try:
    from tzlocal import get_localzone
    import pytz

    TZ_LOCAL = get_localzone()
except Exception:
    pytz = None
    TZ_LOCAL = None

    log.warn('Unable to retrieve system timezone, datetime objects will be returned in local time', exc_info=True)

MODEL_KEYS = {
    MediaItem:              'media',
    MediaPart:              'part',

    MetadataItemSettings:   'settings'
}


class LibraryBase(object):
    def __init__(self, library=None):
        self._library = library

    @property
    def library(self):
        return self._library or Library

    @property
    def matcher(self):
        return self.library.matcher

    @staticmethod
    def settings_directory(rating):
        result = {}

        if rating is not None:
            result['rating'] = rating

        return result

    @staticmethod
    def settings_video(rating, view_offset, last_viewed_at):
        result = {}

        if rating is not None:
            result['rating'] = rating

        if view_offset is not None:
            result['view_offset'] = view_offset

        if last_viewed_at is not None:
            result['last_viewed_at'] = last_viewed_at

        return result

    @staticmethod
    def _tuple_iterator(query):
        result = query.tuples().execute()

        while True:
            try:
                row = result.iterate()
            except UnicodeDecodeError as ex:
                log.warn('Unable to retrieve row: %s', ex, exc_info=True, extra={
                    'event': {
                        'module': __name__,
                        'name': '_tuple_iterator.iterate.unicode_decode_error',
                        'key': '%s:%s' % (ex.encoding, ex.reason)
                    }
                })
                continue

            yield row

    @staticmethod
    def _models(fields, account=None):
        models = {}

        for field in fields:
            model = field.model_class

            # Ensure `model` is only returned once
            if model.__name__ in models:
                continue

            # Test model validity
            if model == MetadataItemSettings and account is None:
                raise ValueError('MetadataItemSettings fields require the "account" parameter')

            # Update `models` dictionary, yield model
            models[model.__name__] = model

            yield model

    @classmethod
    def _join(cls, query, models, account, exclude=None):
        if exclude is None:
            exclude = []

        for model in models:
            if model == MetadataItem:
                continue

            if model in exclude:
                continue

            if model == MetadataItemSettings:
                query = cls._join_settings(query, account)
            elif model == MediaItem:
                query = query.join(
                    MediaItem, JOIN_LEFT_OUTER,
                    on=(MediaItem.metadata_item == MetadataItem.id).alias('media')
                )
            else:
                raise ValueError('Unable to join unknown model: %r' % model)

        return query

    @staticmethod
    def _join_settings(query, account, metadata_model=None):
        if metadata_model is None:
            metadata_model = MetadataItem

        query = query.join(
            MetadataItemSettings, JOIN_LEFT_OUTER,
            on=(
                (MetadataItemSettings.guid == metadata_model.guid) &
                (MetadataItemSettings.account == account)
            ).alias('settings')
        )

        return query

    @classmethod
    def _parse(cls, fields, row, offset=0):
        item = {}

        for x in xrange(offset, len(fields)):
            field = fields[x]
            value = row[x]

            try:
                # Parse field
                value = cls._parse_field(field, value)
            except Exception as ex:
                log.error('Unable to parse value %r as field %r', value, field)
                raise ex

            # Update `item` with field
            if field.model_class in [MetadataItem, Episode]:
                item[field.name] = value
            elif field.model_class in MODEL_KEYS:
                key = MODEL_KEYS[field.model_class]

                if key not in item:
                    item[key] = {}

                item[key][field.name] = value
            else:
                raise ValueError('Unable to parse field %r, unknown model %r' % (field, field.model_class))

        return tuple(list(row[:offset]) + [item])

    @staticmethod
    def _parse_field(field, value):
        if type(field) is FieldProxy:
            field = field.field_instance

        if type(field) is DateTimeField:
            if not value:
               return None

            if isinstance(value, int):
               value = datetime.fromtimestamp(value)
               return TZ_LOCAL.localize(value).astimezone(pytz.utc)

            if not isinstance(value, datetime):
               log.debug('Invalid value provided for DateTimeField: %r (expected datetime instance)', value)
               return None

            if value.tzinfo:
                # `tzinfo` provided, ignore conversion
                return value

            if not TZ_LOCAL or not pytz:
                # Missing "tzlocal" or "pytz" module
                return value

            # Convert datetime to UTC
            return TZ_LOCAL.localize(value).astimezone(pytz.utc)

        return value


class MovieLibrary(LibraryBase):
    def __call__(self, sections, fields=None, account=None, where=None):
        # Set default `select()` fields
        if fields is None:
            fields = []

        fields = [
            MetadataItem.id,
            MetadataItem.guid
        ] + fields

        # Build query
        query = self.query(
            sections,
            fields=fields,
            account=account,
            where=where
        )

        # Parse rows
        return [
            self._parse(fields, row, offset=2)
            for row in self._tuple_iterator(query)
        ]

    def count(self, sections, account=None):
        # Build query
        query = self.query(
            sections, [
                MetadataItem.id
            ],
            account=account
        )

        # Return number of items
        return query.count()

    def query(self, sections, fields=None, account=None, where=None):
        # Retrieve `id` from `Account`
        if account and type(account) is Account:
            account = account.id

        # Map `Section` list to ids
        section_ids = [id for (id, ) in sections]

        # Build `select()` query
        if fields is None:
            fields = []

        # Build `where()` query
        if where is None:
            where = []

        where += [
            MetadataItem.library_section << section_ids,
            MetadataItem.metadata_type == MetadataItemType.Movie
        ]

        # Build query
        return self._join(
            MetadataItem.select(*fields),
            self._models(fields, account),
            account
        ).where(
            *where
        )

    def mapped(self, sections, fields=None, account=None, where=None, parse_guid=False):
        # Retrieve `id` from `Account`
        if account and type(account) is Account:
            account = account.id

        # Map `Section` list to ids
        section_ids = [id for (id, ) in sections]

        # Build `select()` query
        if fields is None:
            fields = []

        # Build subquery
        subq = (Taggings
                        .select(
                            Tags.tag_type,
                            Tags.tag,
                            Taggings.metadata_item
                        )
                        .join(Tags, on=(Tags.id == Taggings.tag).alias('taggings'))
                        .where(Tags.tag_type == 314)
                        .order_by(Tags.id.asc())
                        .switch(Taggings)
                        .alias('subq')
        )

        fields = [
            MetadataItem.id,
            MetadataItem.guid,
            subq.c.tag,

            MediaPart.duration,
            MediaPart.file,

            MetadataItemSettings.rating,
            MetadataItemSettings.view_count,
            MetadataItemSettings.view_offset,
            MetadataItemSettings.last_viewed_at
        ] + fields

        # Build `where()` query
        if where is None:
            where = []

        where += [
            MetadataItem.library_section << section_ids,
            MetadataItem.metadata_type == MetadataItemType.Movie
        ]

        # Build query
        query = (MetadataItem.select(*fields)
                             .join(subq, JOIN_LEFT_OUTER, on=(subq.c.metadata_item_id == MetadataItem.id).alias('tags'))
                             .switch(MetadataItem)
                             .join(MediaItem, on=(MediaItem.metadata_item == MetadataItem.id).alias('media'))
                             .join(MediaPart, on=(MediaPart.media_item == MediaItem.id).alias('part'))
                             .switch(MetadataItem)
        )

        # Join settings
        query = self._join_settings(query, account, MetadataItem)

        # Join extra models
        models = self._models(fields, account)

        query = self._join(query, models, account, [
            MetadataItemSettings,
            MediaItem,
            MediaPart,
            Taggings,
            Tags
        ])

        # Apply `WHERE` filter
        query = query.where(
            *where
        )

        # Iterate over items, parse guid (if enabled)
        guids = {}

        def movies_iterator():
            for row in self._tuple_iterator(query):
                id, guid, tag, movie = self._parse(fields, row, offset=3)

                # Parse `guid` (if enabled, and not already parsed)
                if parse_guid:
                    if id not in guids:
                        if tag:
                            guids[id] = Guid.parse(tag, strict=True)
                        else:
                            guids[id] = Guid.parse(guid, strict=True)
                    guid = guids[id]

                # Return item
                yield id, guid, movie

        return movies_iterator()


class ShowLibrary(LibraryBase):
    def __call__(self, sections, fields=None, account=None, where=None):
        # Set default `select()` fields
        if fields is None:
            fields = []
        
        subq = (Taggings
                .select(
                    Tags.tag_type,
                    Tags.tag,
                    Taggings.metadata_item
                )
                .join(Tags, on=(Tags.id == Taggings.tag).alias('taggings'))
                .where(Tags.tag_type == 314)
                .order_by(Tags.id.asc())
                .switch(Taggings)
                .alias('subq')
        )

        fields = [
            MetadataItem.id,
            MetadataItem.guid,
            subq.c.tag
        ] + fields

        query = (MetadataItem.select(*fields)
                        .join(subq, JOIN_LEFT_OUTER, on=(subq.c.metadata_item_id == MetadataItem.id).alias('tags'))
                        .switch(MetadataItem)
        )

        if account and type(account) is Account:
            account = account.id

        # Map `Section` list to ids
        section_ids = [id for (id, ) in sections]

        # Build `where()` query
        if where is None:
            where = []

        where += [
            MetadataItem.library_section << section_ids,
            MetadataItem.metadata_type == MetadataItemType.Show
        ]

        # Join settings
        query = self._join_settings(query, account, MetadataItem)

        # Join extra models
        models = self._models(fields, account)

        query = self._join(query, models, account, [
            MetadataItemSettings,
            Taggings,
            Tags
        ])

        # Apply `WHERE` filter
        query = query.where(
            *where
        )

        # Parse rows
        return [
            self._parse(fields, row, offset=3)
            for row in self._tuple_iterator(query)
        ]

    def count(self, sections, account=None):
        # Build query
        query = self.query(
            sections, [
                MetadataItem.id
            ],
            account=account
        )

        # Return number of items
        return query.count()

    def query(self, sections, fields=None, account=None, where=None):
        # Retrieve `id` from `Account`
        if account and type(account) is Account:
            account = account.id

        # Map `Section` list to ids
        section_ids = [id for (id, ) in sections]

        # Build `select()` query
        if fields is None:
            fields = []

        # Build `where()` query
        if where is None:
            where = []

        where += [
            MetadataItem.library_section << section_ids,
            MetadataItem.metadata_type == MetadataItemType.Show
        ]

        # Build query
        return self._join(
            MetadataItem.select(*fields),
            self._models(fields, account),
            account
        ).where(
            *where
        )


class SeasonLibrary(LibraryBase):
    def __call__(self, sections, fields=None, account=None, where=None):
        # Retrieve `id` from `Account`
        if account and type(account) is Account:
            account = account.id

        # Map `Section` list to ids
        section_ids = [id for (id, ) in sections]

        # Build `select()` query
        if fields is None:
            fields = []

        fields = [
            MetadataItem.id,
            MetadataItem.index
        ] + fields

        # Build `where()` query
        if where is None:
            where = []

        where += [
            MetadataItem.metadata_type == MetadataItemType.Season,
            MetadataItem.library_section << section_ids
        ]

        # Build query
        query = self._join(
            MetadataItem.select(*fields),
            self._models(fields, account),
            account
        ).where(
            *where
        )

        # Parse rows
        return [
            self._parse(fields, row, offset=1)
            for row in self._tuple_iterator(query)
        ]


class EpisodeLibrary(LibraryBase):
    def __call__(self, sections, fields=None, account=None, where=None):
        # Build `select()` query
        if fields is None:
            fields = []

        fields = [
            MetadataItem.id,
            MetadataItem.index
        ] + fields

        # Build query
        query = self.query(
            sections,
            fields=fields,
            account=account,
            where=where
        )

        # Parse rows
        return [
            self._parse(fields, row, offset=2)
            for row in self._tuple_iterator(query)
        ]

    def count(self, sections):
        # Build query
        query = self.query(sections)

        # Return number of items
        return query.count()

    def count_items(self, sections):
        # Map `Section` list to ids
        section_ids = [id for (id, ) in sections]

        # Build query
        query = MetadataItem.select(
            peewee.fn.sum(MetadataItem.media_item_count)
        ).where(
            MetadataItem.library_section << section_ids,
            MetadataItem.metadata_type == MetadataItemType.Episode
        )

        # Return number of items
        return query.scalar()

    def query(self, sections, fields=None, account=None, where=None):
        # Retrieve `id` from `Account`
        if account and type(account) is Account:
            account = account.id

        # Map `Section` list to ids
        section_ids = [id for (id, ) in sections]

        # Build `select()` query
        if fields is None:
            fields = []

        # Build `where()` query
        if where is None:
            where = []

        where += [
            MetadataItem.library_section << section_ids,
            MetadataItem.metadata_type == MetadataItemType.Episode
        ]

        # Build query
        return self._join(
            MetadataItem.select(*fields),
            self._models(fields, account),
            account
        ).where(
            *where
        )

    def mapped(self, sections, fields=None, account=None, parse_guid=False):
        # Retrieve `id` from `Account`
        if account and type(account) is Account:
            account = account.id

        # Parse `fields`
        if fields is None:
            fields = ([], [], [])

        sh_fields, se_fields, ep_fields = fields

        # Retrieve items
        shows = self.mapped_shows(sections, sh_fields, account)
        seasons = self.mapped_seasons(sections, se_fields, account)
        episodes = self.mapped_episodes(sections, ep_fields, account)

        # Prime `Matcher` cache
        if self.matcher is not None and self.matcher.cache is not None and hasattr(self.matcher.cache, 'prime'):
            context = self.matcher.cache.prime(force=True)
        else:
            context = PrimeContext()

        # Show iterator, parse guid (if enabled)
        guids = {}

        def shows_iterator():
            for sh_id, (guid, show) in shows.items():
                # Parse `guid` (if enabled, and not already parsed)
                if parse_guid:
                    if id not in guids:
                        guids[sh_id] = Guid.parse(guid, strict=True)

                    guid = guids[sh_id]

                yield sh_id, guid, show

        # Episode iterator, parse guid (if enabled)
        def episodes_iterator():
            for sh_id, se_id, ep_id, ep_index, episode in episodes:
                # Retrieve parents
                if sh_id not in shows:
                    log.debug('Unable to find show by id: %r', sh_id)
                    continue

                guid, show = shows[sh_id]

                if se_id not in seasons:
                    log.debug('Unable to find season by id: %r', se_id)
                    continue

                season = seasons[se_id]

                # Parse `guid` (if enabled, and not already parsed)
                if parse_guid:
                    if id not in guids:
                        guids[sh_id] = Guid.parse(guid, strict=True)

                    guid = guids[sh_id]

                # Retrieve episode identifier
                season_num, episode_nums = season['index'], [ep_index]

                # Run `Matcher` on episode (if available)
                if self.matcher is not None:
                    with context:
                        season_num, episode_nums = self.matcher.process_episode(
                            ep_id,
                            (season['index'], ep_index),
                            episode['part']['file']
                        )

                for episode_num in episode_nums:
                    ids = {
                        'show': sh_id,
                        'season': se_id,
                        'episode': ep_id
                    }

                    yield ids, guid, (season_num, episode_num), show, season, episode

        return shows_iterator(), seasons, episodes_iterator()

    def mapped_shows(self, sections, fields=None, account=None):
        # Parse `fields`
        if fields is None:
            fields = []

        fields = [
            MetadataItemSettings.rating
        ] + fields

        # Retrieve shows
        shows = Library.shows(sections, fields, account)
        output = {}
        for (id, guid, tag, show) in shows:
            if id not in output:
                if tag:
                    output[id] = (tag, show)
                else:
                    output[id] = (guid, show)

        # Map shows by `id`
        return output

    def mapped_seasons(self, sections, fields=None, account=None):
        # Parse `fields`
        if fields is None:
            fields = []

        fields = [
            MetadataItemSettings.rating
        ] + fields

        # Retrieve seasons
        seasons = Library.seasons(sections, fields, account)

        # Map seasons by `id`
        return dict([
            (id, show)
            for (id, show) in seasons
        ])

    def mapped_episodes(self, sections, fields=None, account=None, where=None):
        # Map `Section` list to ids
        section_ids = [id for (id, ) in sections]

        # Build `select()` query
        fields = [
            MetadataItem.id,
            Season.id,

            Episode.id,
            Episode.index,

            MediaPart.duration,
            MediaPart.file,

            MetadataItemSettings.rating,
            MetadataItemSettings.view_count,
            MetadataItemSettings.view_offset,
            MetadataItemSettings.last_viewed_at
        ] + fields

        # Build `where()` query
        if where is None:
            where = []

        where += [
            MetadataItem.library_section << section_ids,
            MetadataItem.metadata_type == MetadataItemType.Show
        ]

        # Build query
        query = (MetadataItem.select(*fields)
                             .join(Season, on=(Season.parent == MetadataItem.id).alias('season'))
                             .join(Episode, on=(Episode.parent == Season.id).alias('episode'))
                             .join(MediaItem, on=(MediaItem.metadata_item == Episode.id).alias('media'))
                             .join(MediaPart, on=(MediaPart.media_item == MediaItem.id).alias('part'))
                             .switch(Episode)
        )

        # Join settings
        query = self._join_settings(query, account, Episode)

        # Join extra models
        models = self._models(fields, account)

        query = self._join(query, models, account, [
            MetadataItemSettings,
            MediaItem,
            MediaPart
        ])

        # Apply `WHERE` filter
        query = query.where(
            *where
        )

        def iterator():
            for row in self._tuple_iterator(query):
                yield self._parse(fields, row, offset=4)

        return iterator()


class Library(object):
    matcher = Matcher

    movies = MovieLibrary()
    shows = ShowLibrary()
    seasons = SeasonLibrary()
    episodes = EpisodeLibrary()

    def __init__(self, matcher):
        self.matcher = matcher

        self.movies = MovieLibrary(self)
        self.shows = ShowLibrary(self)
        self.seasons = SeasonLibrary(self)
        self.episodes = EpisodeLibrary(self)

    @classmethod
    def sections(cls, section_type, *fields, **kwargs):
        agent_required = kwargs.pop('agent_required', True)

        filter = []

        if type(section_type) is list:
            # `section_type` is a list
            filter.append(LibrarySection.section_type << section_type)
        else:
            # `section_type` is an integer
            filter.append(LibrarySection.section_type == section_type)

        if agent_required is True:
            # Filter out any sections without metadata agents
            filter.append(LibrarySection.agent != "com.plexapp.agents.none")

        if fields:
            # Return specific fields from table
            return LibrarySection.select(*fields).where(*filter)

        return LibrarySection.select().where(*filter)

    @classmethod
    def media(cls, sections, include_episodes=False):
        pass
