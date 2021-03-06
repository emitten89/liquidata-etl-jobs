import re
import subprocess
import time
import logging
import requests
from collections import defaultdict
from os import path
from pathlib import Path
from doltpy.etl import get_df_table_writer, get_dolt_loader, get_branch_creator
from doltpy.core.dolt import Dolt
import pandas as pd
from typing import Callable
from unidecode import unidecode
from nltk.stem import PorterStemmer

DoltTableLoader = Callable[[Dolt], str]

logger = logging.getLogger(__name__)

CURR_DIR = path.dirname(path.abspath(__file__))
BZ2_FILE_NAME = 'enwiki-latest-pages-articles-multistream.xml.bz2'
DUMP_URL = 'https://dumps.wikimedia.your.org/enwiki/latest/{}'.format(BZ2_FILE_NAME)
WIKIEXTRACTOR_PATH = path.join(Path(CURR_DIR).parent, 'wikiextractor/WikiExtractor.py')

WORD_USES = defaultdict(int)

LINE_TRANS = str.maketrans('–’', "-\'")
WORD_SPLIT = re.compile(r'[^\w\-\'\.&]|[\'\-\'\.&\/_]{2,}')

FILTERS = {
    'none': re.compile(r'^[\w\.\-\/][\w\.\'\-\/&]*[\w\.\-]*$'),
    'no_numbers': re.compile(r'.*[0-9].*'),
    'ASCII_only': re.compile(r'^[a-z0-9\-][a-z0-9\.\'\-&]*[a-z0-9\.\-]$'),
    'no_abbreviations': re.compile(r'.*[&\.].*'),
    'strict': re.compile(r'^[a-z][a-z\'\-]*[a-z\.\']$')
}

FILTER_NAMES = ['no_numbers', 'ASCII_only', 'no_abbreviations', 'strict', 'convert_to_ASCII', 'stemmed']


def fetch_data():
    logging.info('Fetching Wikipedia XML dump from URL {}'.format(DUMP_URL))
    r = requests.get(DUMP_URL, stream=True)
    with open(BZ2_FILE_NAME, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:
                f.write(chunk)
    logging.info('Finished downloading XML dump')
    process_bz2()


def process_bz2():
    logging.info('Processing XML dump')
    start = time.time()

    with subprocess.Popen(
        'bzcat {} | {} --no_templates -o - -'.format(BZ2_FILE_NAME, WIKIEXTRACTOR_PATH),
        stdout=subprocess.PIPE,
        shell=True,
    ) as proc:
        for line in proc.stdout:
            line = line.decode('utf-8')
            line = line.translate(LINE_TRANS)
            if not is_line_tag(line):
                line = line.lower()
                for word in filter(None, WORD_SPLIT.split(line)):
                    word = remove_unwanted_punctuation(word)
                    if FILTERS.get('none').match(word):
                        WORD_USES[word] += 1

    duration = (time.time() - start)/60
    logging.info('ET completed in %.1f minutes', duration)


def is_line_tag(line: str):
    return line[0:4] == '<doc' or '</doc>' in line


def remove_unwanted_punctuation(word: str):
    punctuation = ".'&"
    while len(word) > 0 and (word[0] in punctuation or word[-1] in punctuation):
        if word[0] in punctuation:
            word = word[1:]
        elif word[-1] in punctuation:
            word = word[:-1]
    return word


def passes_filter(filter_type: str, word: str):
    if filter_type[:2] == 'no':
        return not FILTERS.get(filter_type).match(word)
    return FILTERS.get(filter_type).match(word)


def apply_filter(filter_type: str, word: str, porter: PorterStemmer):
    if filter_type == 'stemmed':
        return porter.stem(word), True
    if filter_type == 'convert_to_ASCII':
        return unidecode(word), True
    return word, passes_filter(filter_type, word)


def get_filter_dict(filter_type: str):
    filter_dict = defaultdict(int)
    porter = PorterStemmer()
    for word, frequency in WORD_USES.items():
        word, passed_filter = apply_filter(filter_type, word, porter)
        if passed_filter and word is not None and len(word) > 0:
            filter_dict[word] += frequency
    return filter_dict


def get_master_df_builder() -> Callable[[], pd.DataFrame]:
    def inner() -> pd.DataFrame:
        fetch_data()
        logging.info('Successfully processed {} words from dump'.format(len(WORD_USES.items())))
        df = pd.DataFrame([{'word': word, 'frequency': frequency}
                          for word, frequency in WORD_USES.items()])
        return df.astype({'frequency': 'int'})

    return inner


def get_filter_df_builder(filter_type: str) -> Callable[[], pd.DataFrame]:
    def inner() -> pd.DataFrame:
        filter_dict = get_filter_dict(filter_type)
        logging.info('Successfully processed {} words with {} filter'.format(len(filter_dict.items()), filter_type))
        df = pd.DataFrame([{'word': word, 'frequency': frequency}
                          for word, frequency in filter_dict.items()])
        return df.astype({'frequency': 'int'})

    return inner


def get_wikipedia_loaders(branch_date: str):
    loaders = []
    master_writer = get_df_table_writer('word_frequency',
                                        get_master_df_builder(),
                                        pk_cols=['word'],
                                        import_mode='replace')
    message = 'Update Wikipedia word frequencies for {} XML dump'.format(branch_date)
    loaders.append(get_dolt_loader([master_writer], True, message, 'master'))

    loaders.append(get_branch_creator(branch_date))

    for filter_name in FILTER_NAMES:
        filter_writer = get_df_table_writer('word_frequency',
                                            get_filter_df_builder(filter_name),
                                            pk_cols=['word'],
                                            import_mode='replace')
        branch_name = '{}/filter_{}'.format(branch_date, filter_name)
        filter_message = 'Update Wikipedia word frequencies with {} filter for {} XML dump'.format(branch_date,
                                                                                                   filter_name)
        loaders.append(get_dolt_loader([filter_writer], True, filter_message, branch_name))

    return loaders
