#!/usr/bin/env python3
#
#  Copyright (c) 2019 unfoldingWord
#  http://creativecommons.org/licenses/MIT/
#  See LICENSE file for details.
#
#  Contributors:
#  Richard Mahn <rich.mahn@unfoldingword.org>

"""
Class for any resource PDF converter
"""
import os
import re
import logging
import tempfile
import markdown2
import shutil
import subprocess
import string
import requests
import sys
import argparse
import jsonpickle
from typing import List, Type
from bs4 import BeautifulSoup
from abc import abstractmethod
from weasyprint import HTML, LOGGER
from .resource import Resource, Resources
from .rc_link import ResourceContainerLink
from ..general_tools.file_utils import write_file, read_file, load_json_object

DEFAULT_LANG_CODE = 'en'
DEFAULT_OWNER = 'unfoldingWord'
DEFAULT_TAG = 'master'
LANGUAGE_FILES = {
    'fr': 'French-fr_FR.json',
    'en': 'English-en_US.json'
}
APPENDIX_LINKING_LEVEL = 1
APPENDIX_RESOURCES = ['ta', 'tw']

class PdfConverter:

    def __init__(self, resources: Resources, project_id=None, working_dir=None, output_dir=None,
                 lang_code=DEFAULT_LANG_CODE, regenerate=False, logger=None):
        self.resources = resources
        self.main_resource = self.resources.main
        self.project_id = project_id
        self.working_dir = working_dir
        self.output_dir = output_dir
        self.lang_code = lang_code
        self.regenerate = regenerate
        self.logger = logger

        self.bad_links = {}
        self.rcs = {}
        self.appendix_rcs = {}
        self.all_rcs = {}

        self.images_dir = None
        self.save_dir = None
        self.html_file = None
        self.pdf_file = None
        self.generation_info = {}
        self.translations = {}
        self.remove_working_dir = False
        self.converters_dir = os.path.dirname(os.path.realpath(__file__))

        if not self.logger:
            self.logger = logging.getLogger()
            self.logger.setLevel(logging.DEBUG)
            ch = logging.StreamHandler()
            ch.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(levelname)s - %(message)s')
            ch.setFormatter(formatter)
            self.logger.addHandler(ch)

    def __del__(self):
        if self.remove_working_dir:
            shutil.rmtree(self.working_dir)

    @property
    def name(self):
        return self.main_resource.resource_name

    @property
    def title(self):
        return self.main_resource.title

    @property
    def simple_title(self):
        return self.main_resource.simple_title

    @property
    def version(self):
        return self.main_resource.version

    @property
    def file_id(self):
        project_id_str = f'_{self.project_id}' if self.project_id else ''
        return f'{self.lang_code}_{self.name}{project_id_str}_{self.main_resource.tag}_{self.main_resource.commit}'

    @property
    def project(self):
        if self.project_id:
            project = self.main_resource.find_project(self.project_id)
            if project:
                self.logger.info(f'Project ID: {self.project_id}; Project Title: {self.project_title}')
                return project
            else:
                self.logger.error(f'Project not found: {self.project_id}')
                exit(1)

    @property
    def project_title(self):
        project = self.project
        if project:
            return project.title

    def translate(self, key):
        if not self.translations:
            if self.lang_code not in LANGUAGE_FILES:
                self.logger.error(f'No locale file for {self.lang_code}.')
                exit(1)
            locale_file = os.path.join(self.converters_dir, '..', 'locale', LANGUAGE_FILES[self.lang_code])
            if not os.path.isfile(locale_file):
                self.logger.error(f'No locale file found at {locale_file} for {self.lang_code}.')
                exit(1)
            self.translations = load_json_object(locale_file)
        keys = key.split('.')
        t = self.translations
        for key in keys:
            t = t.get(key, None)
            if t is None:
                # handle the case where the self.translations doesn't have that (sub)key
                self.logger.error(f"No translation for `{key}`")
                exit(1)
                break
        return t

    def add_bad_link(self, source_rc, rc, fix=None):
        if source_rc:
            if source_rc.rc_link not in self.bad_links:
                self.bad_links[source_rc.rc_link] = {}
            if rc.rc_link not in self.bad_links[source_rc.rc_link] or fix:
                self.bad_links[source_rc.rc_link][rc.rc_link] = fix

    def run(self):
        self.setup_dirs()
        self.setup_resources()

        self.html_file = os.path.join(self.output_dir, f'{self.file_id}.html')
        self.pdf_file = os.path.join(self.output_dir, f'{self.file_id}.pdf')

        self.setup_logging_to_file()
        self.determine_if_regeneration_needed()
        self.generate_html()
        self.generate_pdf()

    def setup_dirs(self):
        if not self.working_dir:
            if 'WORKING_DIR' in os.environ:
                self.working_dir = os.environ['WORKING_DIR']
                self.logger.info(f'Using env var WORKING_DIR: {self.working_dir}')
            else:
                self.working_dir = tempfile.mkdtemp(prefix=f'{self.main_resource.repo_name}-')
                self.remove_working_dir = True

        if not self.output_dir:
            if 'OUTPUT_DIR' in os.environ:
                self.output_dir = os.environ['OUTPUT_DIR']
                self.logger.info(f'Using env var OUTPUT_DIR: {self.output_dir}')
            if not self.output_dir:
                self.output_dir = self.working_dir
                self.remove_working_dir = False

        self.images_dir = os.path.join(self.output_dir, 'images')
        if not os.path.isdir(self.images_dir):
            os.makedirs(self.images_dir)

        self.save_dir = os.path.join(self.output_dir, 'save')
        if not os.path.isdir(self.save_dir):
            os.makedirs(self.save_dir)

        css_path = os.path.join(self.converters_dir, 'templates/css')
        subprocess.call(f'ln -sf "{css_path}" "{self.output_dir}"', shell=True)

    def setup_logging_to_file(self):
        LOGGER.setLevel('INFO')  # Set to 'INFO' for debugging
        logger_handler = logging.FileHandler(os.path.join(self.output_dir, f'{self.file_id}_logger.log'))
        self.logger.addHandler(logger_handler)
        logger_handler = logging.FileHandler(os.path.join(self.output_dir, f'{self.file_id}_weasyprint.log'))
        LOGGER.addHandler(logger_handler)

    def generate_html(self):
        if self.regenerate or not os.path.exists(self.html_file):
            self.logger.info(f'Creating HTML file for {self.file_id}...')

            self.logger.info('Generating cover page HTML...')
            cover_html = self.get_cover_html()

            self.logger.info('Generating license page HTML...')
            license_html = self.get_license_html()

            self.logger.info('Generating body HTML...')
            body_html = self.get_body_html()
            self.get_appendix_rcs()
            self.all_rcs = {**self.rcs, **self.appendix_rcs}
            if 'ta' in self.resources:
                body_html += self.get_appendix_html(self.resources['ta'])
            if 'tw' in self.resources:
                body_html += self.get_appendix_html(self.resources['tw'])
            self.logger.info('Fixing links in body HTML...')
            body_html = self.fix_links(body_html)
            body_html = self._fix_links(body_html)
            self.logger.info('Replacing RC links in body HTML...')
            body_html = self.replace_rc_links(body_html)
            self.logger.info('Generating Contributors HTML...')
            body_html += self.get_contributors_html()
            body_html = self.download_all_images(body_html)
            self.logger.info('Generating TOC HTML...')
            toc_html = self.get_toc_html(body_html)

            with open(os.path.join(self.converters_dir, 'templates/template.html')) as template_file:
                html_template = string.Template(template_file.read())
            title = f'{self.title} - v{self.version}'
            link = ''
            personal_styles_file = os.path.join(self.output_dir, f'css/{self.name}_style.css')
            if os.path.isfile(personal_styles_file):
                link = f'<link href="css/{self.name}_style.css" rel="stylesheet">'
            body = '\n'.join([cover_html, license_html, toc_html, body_html])
            html = html_template.safe_substitute(title=title, link=link, body=body)
            write_file(self.html_file, html)

            link_file_name = '_'.join(self.file_id.split('_')[0:-1]) + '.html'
            link_file_path = os.path.join(self.output_dir, link_file_name)
            subprocess.call(f'ln -sf "{self.html_file}" "{link_file_path}"', shell=True)

            self.save_resource_data()
            self.save_bad_links_html()
            self.logger.info('Generated HTML file.')
        else:
            self.logger.info(f'HTML file {self.html_file} is already there. Not generating. Use -r to force regeneration.')

    def generate_pdf(self):
        if self.regenerate or not os.path.exists(self.pdf_file):
            self.logger.info(f'Generating PDF file {self.pdf_file}...')
            weasy = HTML(filename=self.html_file, base_url=f'file://{self.output_dir}/')
            weasy.write_pdf(self.pdf_file)
            self.logger.info('Generated PDF file.')
            self.logger.info(f'PDF file located at {self.pdf_file}')
            link_file_name = '_'.join(self.file_id.split('_')[0:-1]) + '.pdf'
            link_file_path = os.path.join(self.output_dir, link_file_name)
            subprocess.call(f'ln -sf "{self.pdf_file}" "{link_file_path}"', shell=True)
        else:
            self.logger.info(
                f'PDF file {self.pdf_file} is already there. Not generating. Use -r to force regeneration.')

    def save_bad_links_html(self):
        if not self.bad_links:
            bad_links_html = 'NO BAD LINKS!'
        else:
            bad_links_html = '''
<h1>BAD LINKS</h1>
<ul>
'''
            for source_rc_links in sorted(self.bad_links.keys()):
                for rc_links in sorted(self.bad_links[source_rc_links].keys()):
                    line = f'<li>{source_rc_links}: BAD RC - `{rc_links}`'
                    if self.bad_links[source_rc_links][rc_links]:
                        line += f' - change to `{self.bad_links[source_rc_links][rc_links]}`'
                    bad_links_html += f'{line}</li>\n'
            bad_links_html += '''
</ul>
'''

        with open(os.path.join(self.converters_dir, 'templates/template.html')) as template_file:
            html_template = string.Template(template_file.read())
        html = html_template.safe_substitute(title=f'BAD LINKS FOR {self.file_id}', link='', body=bad_links_html)
        bad_links_file = os.path.join(self.output_dir, f'{self.file_id}_bad_links.html')
        write_file(bad_links_file, html)
        self.logger.info(f'BAD LINKS HTML file can be found at {bad_links_file}')

    def setup_resource(self, resource):
        resource.clone(self.working_dir)
        self.generation_info[resource.repo_name] = {'tag': resource.tag, 'commit': resource.commit}
        logo_path = os.path.join(self.images_dir, resource.logo_file)
        if not os.path.isfile(logo_path):
            command = f'cd "{self.images_dir}" && curl -O "{resource.logo_url}"'
            subprocess.call(command, shell=True)

    def setup_resources(self):
        for resource_name, resource in self.resources.items():
            self.setup_resource(resource)

    def determine_if_regeneration_needed(self):
        # check if any commit hashes have changed
        old_info = self.get_previous_generation_info()
        if not old_info:
            self.logger.info(f'Looks like this is a new commit of {self.file_id}. Generating PDF.')
            self.regenerate = True
        else:
            for resource in self.generation_info:
                if resource in old_info and resource in self.generation_info:
                    old_tag = old_info[resource]['tag']
                    new_tag = self.generation_info[resource]['tag']
                    old_commit = old_info[resource]['commit']
                    new_commit = self.generation_info[resource]['commit']
                    if old_tag != new_tag or old_commit != new_commit:
                        self.logger.info(f'Resource {resource} has changed: {old_tag} => {new_tag}, {old_commit} => {new_commit}. REGENERATING PDF.')
                        self.regenerate = True
                else:
                    self.regenerate = True

    def save_resource_data(self):
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        save_file = os.path.join(self.save_dir, f'{self.file_id}_rcs.json')
        write_file(save_file, jsonpickle.dumps(self.rcs))
        save_file = os.path.join(self.save_dir, f'{self.file_id}_appendix_rcs.json')
        write_file(save_file, jsonpickle.dumps(self.appendix_rcs))
        save_file = os.path.join(self.save_dir, f'{self.file_id}_bad_links.json')
        write_file(save_file, self.bad_links)
        save_file = os.path.join(self.save_dir, f'{self.file_id}_generation_info.json')
        write_file(save_file, self.generation_info)

    def get_previous_generation_info(self):
        save_file = os.path.join(self.save_dir, f'{self.file_id}_generation_info.json')
        if os.path.isfile(save_file):
            return load_json_object(save_file)
        else:
            return {}

    def download_all_images(self, html):
        img_dir = os.path.join(self.images_dir, f'{self.main_resource.repo_name}_images')
        os.makedirs(img_dir, exist_ok=True)
        soup = BeautifulSoup(html, 'html.parser')
        for img in soup.find_all('img'):
            if img['src'].startswith('http'):
                url = img['src']
                filename = re.search(r'/([\w_-]+[.](jpg|gif|png))$', url).group(1)
                img['src'] = f'images/{self.main_resource.repo_name}_images/{filename}'
                filepath = os.path.join(img_dir, filename)
                if not os.path.exists(filepath):
                    with open(filepath, 'wb') as f:
                        response = requests.get(url)
                        f.write(response.content)
        return str(soup)

    @abstractmethod
    def get_body_html(self):
        pass

    def get_rc_by_article_id(self, article_id):
        for rc_link, rc in self.all_rcs:
            if rc.article_id == article_id:
                return rc

    def get_toc_html(self, body_html):
        toc_html = f'''
<article id="contents">
    <h1>{self.translate('table_of_contents')}</h1>
'''
        current_level = 0
        soup = BeautifulSoup(body_html, 'html.parser')
        for header in soup.find_all(re.compile(r'^h\d'), {'class': 'section-header'}):
            level = int(header.name[1])
            # Handle closing of ul/li tags or handle the opening of new ul tags
            if level > current_level:
                for l in range(current_level, level):
                    toc_html += '\n<ul>\n'
            elif level < current_level:
                toc_html += '\n</li>\n'
                for l in range(current_level, level, -1):
                    toc_html += '</ul>\n</li>\n'
            elif current_level > 0:
                toc_html += '\n</li>\n'

            article_id = header.get('id')
            if not article_id:
                parent = header.find_parent(['section', 'article'])
                article_id = parent.get('id')
            rc = self.get_rc_by_article_id(article_id)
            toc_html += f'<li>\n<a href="#{rc.article_id}"><span>{rc.toc_title}</span></a>\n'
            current_level = level
        for l in range(current_level, 0, -1):
            toc_html += '</li>\n</ul>\n'
        toc_html += '</article>'
        return toc_html

    def get_cover_html(self):
        if self.project_id:
            project_title_html = f'<h2 id="cover-project">{self.project_title}</h2>'
            version_title_html = f'<h3 id="cover-version">{self.translate("license.version")} {self.version}</h3>'
        else:
            project_title_html = ''
            version_title_html = f'<h2 id="cover-version">{self.translate("license.version")} {self.version}</h2>'
        cover_html = f'''
<article id="main-cover" class="cover">
    <img src="images/{self.main_resource.logo_file}" alt="UTN"/>
    <h1 id="cover-title">{self.title}</h1>
    {project_title_html}
    {version_title_html}
</article>
'''
        return cover_html

    def get_license_html(self):
        license_html = f'''
<article id="license">
    <h1>{self.translate('license.copyrights_and_licensing')}</h1>
'''
        for resource_name, resource in self.resources.items():
            title = resource.title
            version = resource.version
            publisher = resource.publisher
            issued = resource.issued

            license_html += f'''
    <div class="resource-info">
      <div class="resource-title"><strong>{title}</strong></div>
      <div class="resource-date"><strong>{self.translate('license.date')}:</strong> {issued}</div>
      <div class="resource-version"><strong>{self.translate('license.version')}:</strong> {version}</div>
      <div class="resource-publisher"><strong>{self.translate('license.published_by')}:</strong> {publisher}</div>
    </div>
'''
        license_file = os.path.join(self.main_resource.repo_dir, 'LICENSE.md')
        license_html += markdown2.markdown_path(license_file)
        license_html += '</article>'
        return license_html

    def get_contributors_html(self):
        contributors_html = '<section id="contributors" class="no-header">'
        for idx, resource_name in enumerate(self.resources.keys()):
            resource = self.resources[resource_name]
            contributors = resource.contributors
            contributors_list_classes = 'contributors-list'
            if len(contributors) > 10:
                contributors_list_classes += ' more-than-ten'
            elif len(contributors) > 4:
                contributors_list_classes += ' more-than-four'
            contributors_html += f'<div class="{contributors_list_classes}">'
            if idx == 0:
                contributors_html += f'<h1 class="section-header">{self.translate("contributors")}</h1>'
            if len(self.resources) > 1:
                title = resource.title
                contributors_html += f'<h2>{title} {self.translate("contributors")}</h2>'
            for contributor in contributors:
                contributors_html += f'<div class="contributor">{contributor}</div>'
            contributors_html += '</div>'
        contributors_html += '</section>'
        return contributors_html

    @staticmethod
    def get_title_from_html(html):
        soup = BeautifulSoup(html, 'html.parser')
        header = soup.find(re.compile(r'^h\d'))
        if header:
            return header.text
        else:
            return "NO TITLE"

    @staticmethod
    def get_phrases_to_highlight(html, header_tag=None):
        phrases = []
        soup = BeautifulSoup(html, 'html.parser')
        if header_tag:
            headers = soup.find_all(header_tag)
        else:
            headers = soup.find_all(re.compile(r'^h[3-6]'))
        for header in headers:
            phrases.append(header.text)
        return phrases

    def highlight_text(self, text, phrase):
        parts = re.split(r'\s*…\s*|\s*\.\.\.\s*', phrase)
        processed_text = ''
        to_process_text = text
        for idx, part in enumerate(parts):
            if not part.strip():
                continue
            escaped_part = re.escape(part)
            if '<span' in text:
                split_pattern = '(' + re.sub('(\\\\ +)', r'(\\s+|(\\s*</*span[^>]*>\\s*)+)', escaped_part) + ')'
            else:
                split_pattern = '(' + escaped_part + ')'
            split_pattern += '(?![^<]*>)'  # don't match within HTML tags
            splits = re.split(split_pattern, to_process_text, 1)
            processed_text += splits[0]
            if len(splits) > 1:
                highlight_classes = "highlight"
                if len(parts) > 1:
                    highlight_classes += ' split'
                processed_text += f'<span class="{highlight_classes}">{splits[1]}</span>'
                if len(splits) > 2:
                    to_process_text = splits[-1]
        if to_process_text:
            processed_text += to_process_text
        return processed_text

    def highlight_text_with_phrases(self, orig_text, phrases, rc, ignore=[]):
        highlighted_text = orig_text
        phrases.sort(key=len, reverse=True)
        for phrase in phrases:
            new_highlighted_text = self.highlight_text(highlighted_text, phrase)
            if new_highlighted_text != highlighted_text:
                highlighted_text = new_highlighted_text
            elif not ignore or phrase not in ignore:
                if rc not in self.bad_links:
                    self.bad_links[rc] = {
                        'text': orig_text,
                        'notes': []
                    }
                bad_note = {phrase: None}
                alt_phrase = [
                    phrase.replace('‘', "'").replace('’', "'").replace('“', '"').replace('”', '"'),
                    phrase.replace("'", '’').replace('’', '‘', 1).replace('"', '”').replace('”', '“', 1),
                    phrase.replace('‘', "'").replace('’', "'").replace('“', '"').replace('”', '"'),
                    phrase.replace("'", '’').replace('’', '‘', 1).replace('"', '”').replace('”', '“', 1),
                    phrase.replace('“', '"').replace('”', '"'),
                    phrase.replace('"', '”').replace('”', '“', 1),
                    phrase.replace("'", '’').replace('’', '‘', 1),
                    phrase.replace("'", '’'),
                    phrase.replace('’', "'"),
                    phrase.replace('‘', "'")]
                for alt_phrase in alt_phrase:
                    if orig_text != self.highlight_text(orig_text, alt_phrase):
                        bad_note[phrase] = alt_phrase
                        break
                self.bad_links[rc]['notes'].append(bad_note)
        return highlighted_text

    @staticmethod
    def increase_headers(html, increase_depth=1):
        if html:
            for level in range(5, 0, -1):
                new_level = level + increase_depth
                if new_level > 6:
                    new_level = 6
                html = re.sub(rf'<h{level}([^>]*)>\s*(.+?)\s*</h{level}>', rf'<h{new_level}\1>\2</h{new_level}>',
                              html, flags=re.MULTILINE)
        return html

    @staticmethod
    def decrease_headers(html, minimum_header=2, decrease=1):
        if html:
            if minimum_header < 2:
                minimum_header = 2
            for level in range(minimum_header, 6):
                new_level = level - decrease
                if new_level < 1:
                    new_level = 1
                html = re.sub(rf'<h{level}([^>]*)>\s*(.+?)\s*</h{level}>', rf'<h{new_level}\1>\2</h{new_level}>', html,
                              flags=re.MULTILINE)
        return html

    def replace(self, m):
        before = m.group(1)
        rc_link = m.group(2)
        after = m.group(3)
        if rc_link not in self.all_rcs:
            return m.group()
        rc = self.all_rcs[rc_link]
        if (before == '[[' and after == ']]') or (before == '(' and after == ')') or before == ' ' \
                or (before == '>' and after == '<'):
            return f'<a href="#{rc.article_id}">{rc.title}</a>'
        if (before == '"' and after == '"') or (before == "'" and after == "'"):
            return f'#{rc.article_id}'
        self.logger.error(f'FOUND SOME MALFORMED RC LINKS: {m.group()}')
        return m.group()

    def replace_rc(self, match):
        # Replace rc://... rc links according to self.resource_data:
        # Case 1: RC links in double square brackets that need to be converted to <a> elements with articles title:
        #   e.g. [[rc://en/tw/help/bible/kt/word]] => <a href="#tw-kt-word">God's Word</a>
        # Case 2: RC link already in an <a> tag's href, thus preserve its text
        #   e.g. <a href="rc://en/tw/help/bible/kt/word">text</a> => <a href="#tw-kt-word>Text</a>
        # Case 3: RC link without square brackets not in <a> tag's href:
        #   e.g. rc://en/tw/help/bible/kt/word => <a href="#tw-kt-word">God's Word</a>
        # Case 4: RC link for was not referenced by the main content (exists due to a secondary resource referencing it)
        #   e.g. <a href="rc://en/tw/help/bible/names/horeb">Horeb Mountain</a> => Horeb Mountain
        #   e.g. [[rc://en/tw/help/bible/names/horeb]] => Horeb
        # Case 5: Remove other links to resources without text (they weren't directly reference by main content)
        left = match.group(1)
        rc_link = match.group(2)
        right = match.group(3)
        title = match.group(4)
        if rc_link in self.all_rcs:
            rc = self.all_rcs[rc_link]
            if (left and right and left == '[[' and right == ']]') \
                    or (not left and not right):
                # Only if it is a main article or is in the appendix
                if rc.linking_level <= APPENDIX_LINKING_LEVEL:
                    # Case 1 and Case 3
                    return f'<a href="#{rc.article_id}">{rc.title}</a>'
                else:
                    # Case 4:
                    return rc.title
            else:
                if rc.article:
                    # Case 3, left = `<a href="#` and right = `">[text]</a>`
                    return left + rc.article_id + right
                else:
                    # Case 4
                    return title if title else rc.title
        # Case 5
        return title if title else rc_link

    def replace_rc_links(self, text):
        regex = re.compile(r'(\[\[|<a[^>]+href=")*(rc://[/A-Za-z0-9\*_-]+)(\]\]|"[^>]*>(.*?)</a>)*')
        text = regex.sub(self.replace_rc, text)
        return text

    @staticmethod
    def _fix_links(html):
        # Change [[http.*]] to <a href="http\1">http\1</a>
        html = re.sub(r'\[\[http([^\]]+)\]\]', r'<a href="http\1">http\1</a>', html, flags=re.IGNORECASE)

        # convert URLs to links if not already
        html = re.sub(r'([^">])((http|https|ftp)://[A-Za-z0-9\/\?&_\.:=#-]+[A-Za-z0-9\/\?&_:=#-])',
                      r'\1<a href="\2">\2</a>', html, flags=re.IGNORECASE)

        # URLS wth just www at the start, no http
        html = re.sub(r'([^\/])(www\.[A-Za-z0-9\/\?&_\.:=#-]+[A-Za-z0-9\/\?&_:=#-])', r'\1<a href="http://\2">\2</a>',
                      html, flags=re.IGNORECASE)

        return html

    def fix_links(self, html):
        # can be implemented by child class
        return html

    def get_appendix_rcs(self):
        for rc_link, rc in self.rcs.items():
            self.crawl_ta_tw_deep_linking(rc)

    def crawl_ta_tw_deep_linking(self, source_rc: ResourceContainerLink):
        if source_rc.linking_level > APPENDIX_LINKING_LEVEL + 1:
            return
        # get all rc links. the "?:" in the regex means to not leave the (ta|tw) match in the result
        rc_links = re.findall(r'rc://[A-Z0-9_\*-]+/(?:ta|tw)/[A-Z0-9/_\*-]+', source_rc.article, flags=re.IGNORECASE | re.MULTILINE)
        for rc_link in rc_links:
            self.logger.debug(rc_link)
            rc = ResourceContainerLink(rc_link, linking_level=source_rc.linking_level+1)
            rc.lang_code = self.lang_code  # ensure we are staying within the same language
            if rc.rc_link in self.rcs or rc.rc_link in self.appendix_rcs:
                if rc.rc_link in self.rcs:
                    rc = self.rcs[rc.rc_link]
                else:
                    rc = self.appendix_rcs[rc.rc_link]
                if rc.linking_level > source_rc.linking_level + 1:
                    rc.linking_level = source_rc.linking_level + 1
                rc.add_reference(source_rc)
                continue
            if rc.resource not in APPENDIX_RESOURCES:
                continue
            if rc.resource not in self.resources:
                # We don't have this resource in our list of resources, so adding
                resource = Resource(resource_name=rc.resource, repo_name=f'{self.lang_code}_{rc.resource}',
                                    owner=self.main_resource.owner)
                self.setup_resource(resource)
            if rc.resource == 'ta':
                self.get_ta_article_html(rc, source_rc)
            elif rc.resource == 'tw':
                self.get_tw_article_html(rc, source_rc)
            if rc.article:
                self.appendix_rcs[rc.rc_link] = rc
                self.crawl_ta_tw_deep_linking(rc)
            else:
                self.add_bad_link(source_rc, rc)

    def get_appendix_html(self, resource):
        self.logger.info(f'Generating {resource.resource_name} appendix html...')
        html = ''

        filtered_rcs = dict(filter(lambda x: x[1].resource == resource.resource_name and
                                             x[1].linking_level == APPENDIX_LINKING_LEVEL,
                                   self.appendix_rcs.items()))
        sorted_rcs = sorted(filtered_rcs.items(), key=lambda x: x[1].title.lower())
        for item in sorted_rcs:
            rc = item[1]
            if rc.article:
                html += rc.article
        if html:
            html = f'''
        <section id="{self.lang_code}-{resource.resource_name}">
            <div class="resource-title-page">
                <h1 class="section-header">{resource.title}</h1>
            </div>
            {html}
        </section>
        '''
        return html

    def get_ta_article_html(self, rc, source_rc=None):
        file_path = os.path.join(self.resources[rc.resource].repo_dir, rc.project, rc.path, '01.md')
        fix = None
        # File for the article not in the expected place, so look in a few other places based on resource
        if not os.path.isfile(file_path):
            bad_names = {
                'figs-abstractnoun': 'translate/figs-abstractnouns'
            }
            if rc.path in bad_names:
                alt_path = bad_names[rc.path]
                fix = ResourceContainerLink(f'rc://{self.lang_code}/ta/man/{alt_path}')
                file_path = os.path.join(self.resources['ta'].repo_dir, alt_path, '01.md')
        # if we have the file, then we can proceed to process it
        if os.path.isfile(file_path):
            if fix and source_rc:
                self.add_bad_link(source_rc, rc, fix)
            ta_article_html = markdown2.markdown_path(file_path)
            title_file = os.path.join(os.path.dirname(file_path), 'title.md')
            if os.path.isfile(title_file):
                title = read_file(title_file)
            else:
                title = self.get_title_from_html(ta_article_html)
                ta_article_html = re.sub(r'\s*\n*\s*<h\d>[^<]+</h\d>\s*\n*', r'', ta_article_html, 1,
                                         flags=re.IGNORECASE | re.MULTILINE)  # removes the header
            question_file = os.path.join(os.path.dirname(file_path), 'sub-title.md')
            question = ''
            if os.path.isfile(question_file):
                question = read_file(question_file)
            ta_article_html = self.fix_ta_links(ta_article_html, rc.project)
            top_box = ''
            if question:
                top_box = f'''
    <div class="top-box box">
        <div class="ta-question">
            {self.translate('this_page_answers_the_question')}: <em>{question}</em>
        </div>
    </div>
'''
            ta_article_html = f'''
    <article id="{rc.article_id}">
        <h2 class="section-header">{title}</h2>
        {top_box}
        {ta_article_html}
        {self.get_go_back_to_html(rc)}
    </article>
'''
            rc.set_article(ta_article_html)
        else:
            self.logger.error(f'NO TA FILE AT {file_path}')
            self.add_bad_link(source_rc, rc)

    def get_go_back_to_html(self, source_rc):
        references = []
        for rc_link in source_rc.references:
            rc = self.rcs[rc_link]
            if rc.linking_level < APPENDIX_LINKING_LEVEL:
                references.append(f'<a href="#{rc.article_id}">{rc.title}</a>')
        go_back_to_html = ''
        if len(references):
            references_str = '; '.join(references)
            go_back_to_html = f'''
    <p class="go-back">
        (<b>{self.translate('go_back_to')}:</b> {references_str})
    </p>
'''
        return go_back_to_html

    def fix_ta_links(self, text, manual):
        text = re.sub(r'href="\.\./([^/"]+)/01\.md"', rf'href="rc://{self.lang_code}/ta/man/{manual}/\1"', text,
                      flags=re.IGNORECASE | re.MULTILINE)
        text = re.sub(r'href="\.\./\.\./([^/"]+)/([^/"]+)/01\.md"', rf'href="rc://{self.lang_code}/ta/man/\1/\2"', text,
                      flags=re.IGNORECASE | re.MULTILINE)
        text = re.sub(r'href="([^# :/"]+)"', rf'href="rc://{self.lang_code}/ta/man/{manual}/\1"', text,
                      flags=re.IGNORECASE | re.MULTILINE)
        return text

    def get_tw_article_html(self, rc, source_rc=None):
        file_path = os.path.join(self.resources[rc.resource].repo_dir, rc.project, f'{rc.path}.md')
        fix = None
        if not os.path.exists(file_path):
            bad_names = {
                'live': 'bible/kt/life'
            }
            if rc.extra_information[-1] in bad_names:
                path2 = bad_names[rc.extra_information[-1]]
            elif rc.path.startswith('bible/other/'):
                path2 = re.sub(r'^bible/other/', r'bible/kt/', rc.path)
            else:
                path2 = re.sub(r'^bible/kt/', r'bible/other/', rc.path)
            fix = 'rc://{0}/tw/dict/{1}'.format(self.lang_code, path2)
            file_path = os.path.join(self.resources[rc.resource].repo_dir, rc.project, f'{path2}.md')
        if os.path.isfile(file_path):
            if fix:
                self.add_bad_link(source_rc, rc, fix)
            if rc.rc_link not in self.appendix_rcs:
                tw_article_html = markdown2.markdown_path(file_path)
                tw_article_html = self.increase_headers(tw_article_html)
                tw_article_html = self.fix_tw_links(tw_article_html, rc.extra_information[0])
                tw_article_html = f'''                
    <article id="{rc.article_id}">
        {tw_article_html}
        {self.get_go_back_to_html(rc)}
    </article>'''
                rc.set_article(tw_article_html)
        else:
            if source_rc.rc_link not in self.bad_links:
                self.bad_links[source_rc.rc_link] = {}
            if rc.rc_link not in self.bad_links[source_rc.rc_link]:
                self.bad_links[source_rc.rc_link][rc.rc_link] = None

    def fix_tw_links(self, text, group):
        text = re.sub(r'href="\.\./([^/)]+?)(\.md)*"', rf'href="rc://{self.lang_code}/tw/dict/bible/{group}/\1"', text,
                      flags=re.IGNORECASE | re.MULTILINE)
        text = re.sub(r'href="\.\./([^)]+?)(\.md)*"', rf'href="rc://{self.lang_code}/tw/dict/bible/\1"', text,
                      flags=re.IGNORECASE | re.MULTILINE)
        text = re.sub(r'(\(|\[\[)(\.\./)*(kt|names|other)/([^)]+?)(\.md)*(\)|\]\])(?!\[)',
                      rf'[[rc://{self.lang_code}/tw/dict/bible/\3/\4]]', text,
                      flags=re.IGNORECASE | re.MULTILINE)
        return text


def run_converter(resource_names: List[str], pdf_converter_class: Type[PdfConverter]):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-l', '--lang_code', dest='lang_codes', required=False, help='Language Code(s)',
                        action='append')
    parser.add_argument('-p', '--project_id', dest='project_ids', required=False, help='Project ID(s)', action='append')
    parser.add_argument('-w', '--working', dest='working_dir', default=False, required=False, help='Working Directory')
    parser.add_argument('-o', '--output', dest='output_dir', default=False, required=False, help='Output Directory')
    parser.add_argument('--owner', dest='owner', default=DEFAULT_OWNER, required=False, help='Owner')
    parser.add_argument('-r', '--regenerate', dest='regenerate', action='store_true',
                        help='Regenerate PDF even if exists')
    for resource_name in resource_names:
        parser.add_argument(f'--{resource_name}-tag', dest=resource_name, default=DEFAULT_TAG, required=False)

    args = parser.parse_args(sys.argv[1:])
    lang_codes = args.lang_codes
    project_ids = args.project_ids
    working_dir = args.working_dir
    output_dir = args.output_dir
    owner = args.owner
    regenerate = args.regenerate
    if not lang_codes:
        lang_codes = [DEFAULT_LANG_CODE]
    if not project_ids:
        project_ids = [None]

    resources = Resources()
    for lang_code in lang_codes:
        for project_id in project_ids:
            for resource_name in resource_names:
                repo_name = f'{lang_code}_{resource_name}'
                tag = getattr(args, resource_name)
                resource = Resource(resource_name=resource_name, repo_name=repo_name, tag=tag, owner=owner)
                resources[resource_name] = resource
            converter = pdf_converter_class(resources=resources, project_id=project_id, working_dir=working_dir,
                                            output_dir=output_dir, lang_code=lang_code, regenerate=regenerate)
            project_id_str = f'_{project_id}' if project_id else ''
            converter.logger.info(f'Starting PDF Converter for {resources.main.repo_name}_{resources.main.tag}{project_id_str}...')
            converter.run()


