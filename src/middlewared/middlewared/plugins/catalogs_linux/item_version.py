import errno
import os

from middlewared.schema import accepts, Bool, Dict, List, returns, Str
from middlewared.service import CallError, Service


class CatalogService(Service):

    class Config:
        cli_namespace = 'app.catalog'

    @accepts(
        Str('item_name'),
        Dict(
            'item_version_details',
            Bool('cache', default=True),
            Str('catalog', required=True),
            Str('train', required=True),
        )
    )
    @returns(Dict(
        'item_details',
        Str('name', required=True),
        List('categories', items=[Str('category')], required=True),
        Str('app_readme', null=True, required=True),
        Str('location', required=True),
        Bool('healthy', required=True),
        Str('healthy_error', required=True, null=True),
        Str('healthy_error', required=True, null=True),
        Dict('versions', required=True, additional_attrs=True),
        Str('latest_version', required=True, null=True),
        Str('latest_app_version', required=True, null=True),
        Str('latest_human_version', required=True, null=True),
        Str('icon_url', required=True, null=True),
    ))
    def get_item_details(self, item_name, options):
        """
        Retrieve information of `item_name` `item_version_details.catalog` catalog item.
        """
        catalog = self.middleware.call_sync('catalog.get_instance', options['catalog'])
        item_location = os.path.join(catalog['location'], options['train'], item_name)
        if not os.path.exists(item_location):
            raise CallError(f'Unable to locate {item_name!r} at {item_location!r}', errno=errno.ENOENT)
        elif not os.path.isdir(item_location):
            raise CallError(f'{item_location!r} must be a directory')

        if options['cache'] and self.middleware.call_sync(
            'cache.has_key', f'catalog_{options["catalog"]}_train_details'
        ):
            cached_data = self.middleware.call_sync('cache.get', f'catalog_{options["catalog"]}_train_details')
            if item := cached_data.get(options['train'], {}).get(item_name):
                # We need to update enums for refs in schema, cannot rely on cache for the latest values. Those
                # refer to fields in schema showing us the available interfaces GPUs, Certificates, CAs etc.
                for version in item['versions']:
                    versioned_item = item['versions'][version]
                    needs_normalization = versioned_item['healthy'] and versioned_item['required_features'] and any(
                        feature.startswith('definitions/')
                        for feature in versioned_item['required_features']
                    )
                    if needs_normalization:
                        questions_context = self.middleware.call_sync('catalog.get_normalised_questions_context')
                        self.middleware.call_sync('catalog.normalise_questions', versioned_item, questions_context)
                return item

        return self.middleware.call_sync('catalog.retrieve_item_details', item_location)
