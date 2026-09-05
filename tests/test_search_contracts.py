"""Offline checks for pagination and the documented result-shape boundaries."""
import unittest
from unittest.mock import patch

import brave
import exa


class PaginationTests(unittest.TestCase):
    def test_consecutive_pages_for_multiple_page_sizes(self):
        for size in (1, 5, 20):
            with self.subTest(size=size):
                offsets = []

                def page(query, **kw):
                    offset = kw['offset']
                    offsets.append(offset)
                    return {'results': [
                        {'url': f'https://example.org/{offset * size + i}'}
                        for i in range(size)
                    ], 'query': {'more_results_available': True}}

                with patch.object(brave, '_search_full', side_effect=page):
                    result = brave.paged('topic', count=size, total=3 * size, max_pages=3)
                self.assertEqual(offsets, [0, 1, 2])
                self.assertEqual(result['n'], 3 * size)

    def test_overlap_does_not_skip_following_page(self):
        responses = [
            {'results': [{'url': 'https://example.org/a'}], 'query': {'more_results_available': True}},
            {'results': [{'url': 'https://example.org/a'}], 'query': {'more_results_available': True}},
            {'results': [{'url': 'https://example.org/b'}], 'query': {'more_results_available': False}},
        ]
        with patch.object(brave, '_search_full', side_effect=responses) as search:
            result = brave.paged('topic', count=20, total=10)
        self.assertEqual([call.kwargs['offset'] for call in search.call_args_list], [0, 1, 2])
        self.assertEqual(result['n'], 2)
        self.assertTrue(result['exhausted'])

    def test_page_window_and_provider_stop(self):
        with patch.object(brave, '_search_full', return_value={
            'results': [{'url': 'https://example.org/a'}],
            'query': {'more_results_available': True},
        }) as search:
            result = brave.paged('topic', count=20, total=100, max_pages=30)
        self.assertEqual([call.kwargs['offset'] for call in search.call_args_list], list(range(10)))
        self.assertTrue(result['exhausted'])
        with patch.object(brave, '_search_full', return_value={
            'results': [{'url': 'https://example.org/a'}],
            'query': {'more_results_available': False},
        }) as search:
            result = brave.paged('topic', total=100)
        self.assertEqual(search.call_count, 1)
        self.assertTrue(result['exhausted'])


class ResultContractTests(unittest.TestCase):
    def test_brave_compact_and_full_views(self):
        payload = {'mode': 'web', 'query': {'original': 'topic'},
                   'results': [{'title': 'Title', 'url': 'https://example.org',
                                'description': 'Evidence'}],
                   'top_results': [{'type': 'web'}], 'mixed': {'main': []}}
        with patch.object(brave, '_search_full', return_value=payload):
            compact = brave.search('topic')
            full = brave.search('topic', view='full')
        self.assertEqual(compact['results'][0]['snippet'], 'Evidence')
        self.assertEqual(compact['query'], 'topic')
        self.assertNotIn('top_results', compact)
        self.assertIn('top_results', full)

    def test_exa_fetch_envelope_includes_failed_urls(self):
        payload = {'results': [{'id': 'https://example.org/a', 'url': 'https://example.org/a',
                                'title': 'Title', 'text': 'Evidence'}],
                   'statuses': [{'id': 'https://example.org/b', 'status': 'error',
                                 'error': {'tag': 'CRAWL_FAILED'}}]}
        with patch.object(exa, '_get_api_key', return_value='fixture'), \
             patch.object(exa, '_request', return_value=payload):
            result = exa.fetch(urls=['https://example.org/a', 'https://example.org/b'],
                               include_meta=True, max_characters=6000)
        self.assertEqual(len(result['results']), 2)
        self.assertTrue(result['results'][1]['error'])

    def test_webset_yes_filter_is_any_criterion(self):
        item = {'evaluations': [{'satisfied': 'yes'}, {'satisfied': 'no'}]}
        with patch.object(exa, '_get_api_key', return_value='fixture'), \
             patch.object(exa, '_request', return_value={'data': [item]}):
            result = exa.webset_items('fixture', satisfied='yes')
        self.assertEqual(result['data'], [item])


if __name__ == '__main__':
    unittest.main()
