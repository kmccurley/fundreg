#!/usr/bin/env python

"""
Main driver for the index build. Note that running this will not
overwrite an existing index specified in args.dbpath. If you are
replacing an existing database, then write it to a temporary location
and then move the directory to the production directory.

This is able to parse both crossref Funder registry and ROR data.

"""

import argparse
import json
from naya import tokenize, stream_array
from pathlib import Path
import os
import requests
import sys
import xapian
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from model import Funder, FunderList, RelationshipType, DataSource
from rdf_parser import parse_rdf
from search_lib import index_funder

assert sys.version_info >= (3,0)

def create_index(dbpath, funderlist, verbose=False):
    db = xapian.WritableDatabase(dbpath, xapian.DB_CREATE_OR_OPEN)

    # Set up a TermGenerator that we'll use in indexing.
    termgenerator = xapian.TermGenerator()
    termgenerator.set_database(db)
    # use Porter's 2002 stemmer
    termgenerator.set_stemmer(xapian.Stem("english")) 
    termgenerator.set_flags(termgenerator.FLAG_SPELLING);
    count = 0
    for funder in funderlist.funders.values():
        narrower = {}
        index_funder(funder, db, termgenerator)
        count += 1
        if count % 5000 == 0:
            print(f'{count} funders')
            db.commit()
    db.commit()
    print(f'Indexed {count} documents')

def fetch_fundreg():
    print('fetching data/registry.rdf file...')
    url = 'https://gitlab.com/crossref/open_funder_registry/-/raw/master/registry.rdf?inline=false'
    response = requests.get(url)
    rdf_file = Path('data/registry.rdf')
    rdf_file.write_text(response.text, encoding='UTF-8')
    print('updated registry.rdf file')

def fetch_ror():
    print('fetching ROR data')
    response = requests.get('https://zenodo.org/api/records/?communities=ror-data&sort=mostrecent')
    version_data = response.json().get('hits').get('hits')[0]
    print(json.dumps(version_data, indent=2))
    publication_date = version_data.get('metadata').get('publication_date')
    print('ROR data from {}'.format(publication_date))
    latest_url = version_data.get('files')[0].get('links').get('self')
    print('fetching {}'.format(latest_url))
    # latest_url should be a zip file.
    with requests.get(latest_url, stream=True) as stream:
        stream.raise_for_status()
        with open('data/latest.ror.zip', 'wb') as f:
            for chunk in stream.iter_content(chunk_size=8192):
                f.write(chunk)
    with ZipFile('data/latest.ror.zip', 'r') as zipObj:
        filename = zipObj.infolist()[0].filename
        zipObj.extractall()
        os.rename(filename, 'data/raw_ror.json')

def extract_ror_id(uri):
    """Extract 0abcdefg12 from https://ror.org/0abcdefg12"""
    return uri.split('/')[-1]

def parse_ror(filename):
    """Return a map from id to potential Funder from ROR."""
    fp = open(filename, 'r')
    funderslist = FunderList(funders={})
    items = stream_array(tokenize(fp))
    count = 0
    for item in items:
        count += 1
        if count % 5000 == 0:
            print('read {} ror entries'.format(count))
        ror = item.get('id')
        id = extract_ror_id(ror)
        org = {'source_id': id,
               'source': 'ror',
               'name': item.get('name'),
               'altnames': item.get('aliases'),
               'country_code': item.get('country').get('country_code'),
               'country': item.get('country').get('country_name'),
               'children': [],
               'parents': [],
               'related': []
               }
        if len(item.get('types')) > 0:
            org['funder_type'] = item.get('types')[0]
        else:
            org['funder_type'] = 'Other' 
        org['altnames'].extend(item.get('acronyms'))
        org['altnames'].extend([l['label'] for l in item.get('labels')])
        external_ids = item.get('external_ids')
        if external_ids:
            fundref = external_ids.get('FundRef')
            if fundref:
                preferred = fundref.get('preferred')
                if preferred:
                    org['preferred_fundref'] = preferred
        for rel in item['relationships']:
            if rel['type'] == RelationshipType.RELATED.value:
                org['related'].append({'source': DataSource.ROR.value,
                                       'source_id': extract_ror_id(rel['id']),
                                       'name': rel['label']})
            elif rel['type'] == RelationshipType.CHILD:
                org['children'].append({'source': DataSource.ROR.value,
                                       'source_id': extract_ror_id(rel['id']),
                                       'name': rel['label']})
            elif rel['type'] == RelationshipType.PARENT:
                org['parents'].append({'source': DataSource.ROR.value,
                                       'source_id': extract_ror_id(rel['id']),
                                       'name': rel['label']})
        funder = Funder(**org)
        funderslist.funders[funder.global_id()] = funder
    return funderslist

if __name__ == '__main__':
    arguments = argparse.ArgumentParser()
    arguments.add_argument('--verbose',
                           action='store_true',
                           help='Whether to print debug info')
    arguments.add_argument('--include_fundreg',
                           action='store_true',
                           help='Whether to include fundreg (recommended)')
    arguments.add_argument('--fetch_fundreg',
                           action='store_true',
                           help='Whether to fetch a fresh copy with the crossref API')
    arguments.add_argument('--use_cache',
                           action='store_true',
                           help='Use pre-parsed documents')
    arguments.add_argument('--fetch_ror',
                           action='store_true',
                           help='Whether to refetch the json file for ROR')
    arguments.add_argument('--dbpath',
                           default='xapian.db',
                           help='Path to writable database directory.')
    arguments.add_argument('--include_ror',
                           action='store_true',
                           help='Whether to omit ROR data')
    arguments.add_argument('--defer_to_fundreg',
                           action='store_true',
                           help='Whether to replace ROR IDs by related fundreg ID')
    args = arguments.parse_args()
    funders = {}
    outdated = []
    funders_file = Path('data/registry.json')
    ror_file = Path('data/ror.json')
    raw_ror_file = Path('data/raw_ror.json')
    country_map = json.loads(open('data/country_map.json', 'r').read())
    funderlist = FunderList(funders={})
    if os.path.isfile(args.dbpath) or os.path.isdir(args.dbpath):
        print('CANNOT OVERWRITE dbpath')
        sys.exit(2)
    if not args.include_fundreg and not args.include_ror:
        print('To build an index, you need either --include_fundreg and/or --include_ror')
        sys.exit(3)
    if args.fetch_fundreg:
        print('updating fundref.rdf...')
        fetch_fundreg()
    if args.fetch_ror:
        print('fetching ror data...')
        fetch_ror()
    if args.include_fundreg:
        if args.use_cache:
            print('reading {}'.format(funders_file.name))
            funderlist = FunderList.parse_raw(funders_file.read_text(encoding='UTF-8'))
        else:
            print('parsing data/registryrdf for fundreg...')
            funderlist = parse_rdf(country_map)
            funders_file.write_text(funderlist.json(indent=2), encoding='UTF-8')
    if args.include_ror:
        ror_funders = FunderList(funders={})
        if args.use_cache:
            print('reading {}'.format(ror_file.name))
            ror_funders = FunderList.parse_raw(ror_file.read_text(encoding='UTF-8'))
        else:
            print('parsing data/raw_ror.json...this is slow to parse 112000 entries...')
            ror_funders = parse_ror('data/raw_ror.json')
            print('saving cache in data/ror.json')
            ror_file.write_text(ror_funders.json(indent=2), encoding='UTF-8')
        # For now simply add them without merging.
        for key, value in ror_funders.funders.items():
            if args.defer_to_fundreg and value.preferred_fundref:
                preferred_fundreg = '{}_{}'.format(DataSource.FUNDREG.value, value.preferred_fundref)
                preferred_fundreg = funderlist.funders.get(preferred_fundreg)
                if not preferred_fundreg: # unlikely
                    funderlist.funders[key] = value
            else:
                funderlist.funders[key] = value

    if args.verbose:
        print('creating index')
    create_index(args.dbpath, funderlist, args.verbose)

