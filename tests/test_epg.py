import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import urllib.request

import sync_epg as epg

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def xml(programmes=None):
    rows = programmes if programmes is not None else '''
      <programme channel="SkySportCalcio.it" start="20261105200000 +0000" stop="20261105220000 +0000">
        <title>Inter - Napoli (diretta)</title><desc>Calcio Serie A</desc><category>Calcio</category>
      </programme>'''
    return ('''<?xml version="1.0" encoding="UTF-8"?><tv>
      <channel id="SkySportCalcio.it"><display-name>IT - Sky Sport Calcio</display-name></channel>
      <channel id="SkyCalcio1.it"><display-name>IT - Sky Calcio 1</display-name></channel>
      <channel id="SkySportF1.it"><display-name>IT - Sky Sport F1</display-name></channel>
      <channel id="DAZN1.it"><display-name>IT - DAZN 1</display-name></channel>
      <channel id="ZONADAZN.it"><display-name>IT - ZONA DAZN</display-name></channel>
      <channel id="Rai1.it"><display-name>IT - Rai 1</display-name></channel>
      <channel id="TV8.it"><display-name>IT - TV8</display-name></channel>
      <channel id="Eurosport.it"><display-name>IT - Eurosport</display-name></channel>
      <channel id="Canale5.it"><display-name>IT - Canale 5</display-name></channel>
    ''' + rows + '</tv>').encode('utf-8')


def programme(title, **fields):
    return dict({
        "id": "p1", "channelId": "SkySportCalcio.it", "startUtc": "2026-11-05T19:45:00Z",
        "endUtc": "2026-11-05T22:00:00Z", "title": title, "subtitle": "", "description": "Serie A",
        "competitions": ["serie-a"], "broadcastType": "live", "matchEligible": True,
    }, **fields)


def fixture(**fields):
    return dict({"fixtureId": "serie-a:1", "calendarId": "1", "competition": "serie-a", "homeTeam": "Inter",
                 "awayTeam": "Napoli", "kickoffUtc": "2026-11-05T20:00:00Z"}, **fields)


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.config = epg.read_json(ROOT / 'config.json')
        self.aliases = epg.read_json(ROOT / 'aliases/teams.json')

    def parse(self, data):
        return epg.parse_epg(data, self.config, 'it', NOW, {})

    def match(self, rows, fixtures=None):
        return epg.associate(rows, fixtures or [fixture()], self.aliases, self.config)

    def test_only_requested_families_are_followed(self):
        channels, rows, invalid = self.parse(xml())
        self.assertEqual({c['id'] for c in channels}, {'SkySportCalcio.it', 'SkyCalcio1.it', 'SkySportF1.it', 'DAZN1.it', 'ZONADAZN.it', 'Rai1.it', 'TV8.it'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(invalid, 0)

    def test_channel_include_and_exclude_are_configurable(self):
        self.config['channel_ids_include'] = ['Eurosport.it']
        self.config['channel_ids_exclude'] = ['Rai1.it']
        ids = {c['id'] for c in self.parse(xml())[0]}
        self.assertIn('Eurosport.it', ids)
        self.assertNotIn('Rai1.it', ids)

    def test_non_football_is_filtered_even_on_followed_channels(self):
        _, rows, _ = self.parse(xml('''
        <programme channel="Rai1.it" start="20261105200000 +0000" stop="20261105210000 +0000"><title>Telegiornale</title><category>News</category></programme>
        <programme channel="SkySportF1.it" start="20261105200000 +0000" stop="20261105210000 +0000"><title>Formula 1 World Championship</title><category>Sport</category></programme>'''))
        self.assertEqual(rows, [])

    def test_unknown_does_not_become_live_automatically(self):
        self.assertEqual(epg.programme_type('Inter - Napoli', 'Serie A'), 'unknown')

    def test_replay_wins_over_live_word(self):
        self.assertEqual(epg.programme_type('Replica Inter - Napoli', 'Rivedi la diretta', live=True), 'replay')
        self.assertEqual(epg.programme_type('Inter - Napoli', 'Differita calcio'), 'replay')
        self.assertEqual(epg.programme_type('Inter - Napoli', '', previously_shown=True), 'replay')

    def test_explicit_direct_and_xml_live_flags(self):
        self.assertEqual(epg.programme_type('Inter - Napoli (diretta)', ''), 'live')
        self.assertEqual(epg.programme_type('Inter - Napoli', '', live=True), 'live')

    def test_xmltv_offsets_are_used(self):
        self.assertEqual(epg.xml_time('20261105210000 +0100').isoformat(), '2026-11-05T20:00:00+00:00')
        with self.assertRaises(ValueError):
            epg.xml_time('20261105210000')

    def test_rome_window_handles_both_dst_boundaries(self):
        self.config['days_ahead'] = 0
        for month, day, hours in ((3, 29, 23), (10, 25, 25)):
            start, end = epg.window(datetime(2026, month, day, 12, tzinfo=timezone.utc), self.config)
            self.assertEqual((end-start).total_seconds(), hours * 3600)

    def test_invalid_times_are_counted_and_programmes_are_deduplicated(self):
        raw = xml()
        row = raw[raw.index(b'<programme'):raw.index(b'</programme>')+len(b'</programme>')]
        duplicated = raw.replace(b'</tv>', row + row.replace(b'20261105200000 +0000', b'bad') + b'</tv>')
        _, rows, invalid = self.parse(duplicated)
        self.assertEqual(len(rows), 1)
        self.assertEqual(invalid, 1)

    def test_dtd_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(b'<!DOCTYPE tv><tv/>')

    def test_public_epg_version_redirect_is_allowed_but_cross_host_is_rejected(self):
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b'<tv/>'
            epg.fetch('https://iptv-epg.org/files/epg-it.xml')
            handler = build.call_args.args[0]
        req = urllib.request.Request('https://iptv-epg.org/files/epg-it.xml')
        redirected = handler.redirect_request(req, None, 302, '', {}, 'https://iptv-epg.org/files/version/epg-it.xml')
        self.assertEqual(redirected.full_url, 'https://iptv-epg.org/files/version/epg-it.xml')
        for url in ('http://iptv-epg.org/file.xml', 'https://other.example/file.xml'):
            with self.assertRaises(ValueError):
                handler.redirect_request(req, None, 302, '', {}, url)

    def test_calendar_token_never_follows_redirects_or_goes_to_epg(self):
        with self.assertRaises(ValueError):
            epg.fetch('https://iptv-epg.org/files/epg-it.xml', token='private-token')
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b'{}'
            epg.fetch('https://api.github.com/repos/example/repo/contents/calendar.json', token='private-token')
            handler = build.call_args.args[0]
        req = urllib.request.Request('https://api.github.com/repos/example/repo/contents/calendar.json')
        self.assertIsNone(handler.redirect_request(req, None, 302, '', {}, 'https://other.example/file.json'))

    def test_two_teams_and_correct_time_produce_association(self):
        broadcasts = self.match([programme('Internazionale - SSC Napoli')])[0]['broadcasts']
        self.assertEqual(len(broadcasts), 1)
        self.assertEqual(broadcasts[0]['confidence'], 'high')

    def test_only_one_team_is_not_enough(self):
        self.assertEqual(self.match([programme('Inter - Milan')])[0]['broadcasts'], [])

    def test_description_association_has_lower_confidence(self):
        row = programme('Serie A', description='Internazionale contro Napoli')
        self.assertEqual(self.match([row])[0]['broadcasts'][0]['confidence'], 'medium')

    def test_replay_or_wrong_time_or_other_competition_are_not_suggested(self):
        for fields in ({'broadcastType': 'replay'}, {'startUtc': '2026-11-06T20:00:00Z'}, {'competitions': ['serie-b']}, {'description': 'Women Serie A'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.match([programme('Inter - Napoli', **fields)])[0]['broadcasts'], [])

    def test_unknown_is_preserved_in_associations(self):
        self.assertEqual(self.match([programme('Inter - Napoli', broadcastType='unknown')])[0]['broadcasts'][0]['broadcastType'], 'unknown')

    def test_studio_and_simultaneous_ambiguous_fixtures_are_not_associated(self):
        self.assertEqual(self.match([programme('Studio Inter - Napoli', matchEligible=False)])[0]['broadcasts'], [])
        fixtures = [fixture(), fixture(fixtureId='serie-a:2', homeTeam='Roma', awayTeam='Milan')]
        result = self.match([programme('Inter - Napoli e Roma - Milan')], fixtures)
        self.assertTrue(all(not f['broadcasts'] for f in result))

    def test_utf8_team_aliases(self):
        fixtures = [fixture(homeTeam='Fenerbahçe', awayTeam='Liverpool')]
        self.assertEqual(len(self.match([programme('Fenerbahce - Liverpool')], fixtures)[0]['broadcasts']), 1)

    def test_calendar_timestamp_and_missing_kickoff(self):
        doc = {'matches': [dict(id='1', homeTeam='Inter', awayTeam='Napoli', timestamp=1793908800), dict(id='2', homeTeam='Roma', awayTeam='Milan', date='2026-11-05')]}
        rows = epg.parse_calendar(doc, 'serie-a', NOW, self.config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kickoffUtc'], '2026-11-05T20:00:00Z')

    def test_source_failure_preserves_catalogue_and_reports_error(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            download = lambda url: (xml(), 'Wed, 04 Nov 2026 23:12:07 GMT')
            self.assertEqual(epg.run(ROOT/'config.json', output, NOW, downloader=download), 0)
            original = (output/'data/programmes.json').read_bytes()
            with patch.object(epg, 'parse_epg', side_effect=ValueError('broken feed')):
                self.assertEqual(epg.run(ROOT/'config.json', output, NOW, downloader=download), 1)
            self.assertEqual((output/'data/programmes.json').read_bytes(), original)
            self.assertEqual(epg.read_json(output/'data/status.json')['state'], 'error')

    def test_calendar_failure_keeps_prior_matches_but_updates_epg(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            download = lambda url: (xml(), None)
            epg.run(ROOT/'config.json', output, NOW, downloader=download)
            original = (output/'data/matches.json').read_bytes()
            with patch.object(epg, 'load_calendars', side_effect=ValueError('calendar unavailable')):
                self.assertEqual(epg.run(ROOT/'config.json', output, NOW, downloader=download), 1)
            self.assertEqual((output/'data/matches.json').read_bytes(), original)
            self.assertEqual(epg.read_json(output/'data/status.json')['calendarState'], 'error')

    def test_output_contains_no_streams_or_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            epg.run(ROOT/'config.json', output, NOW, downloader=lambda url: (xml(), None))
            for path in output.rglob('*'):
                if path.is_file():
                    content = path.read_text(encoding='utf-8')
                    self.assertNotIn('streamUrl', content)
                    self.assertNotIn('Bearer', content)


if __name__ == '__main__':
    unittest.main()
