from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_node(source: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the Chrome extension regression harness")
    result = subprocess.run(
        [node],
        input=textwrap.dedent(source),
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_rendered_page_media_detector_is_conservative() -> None:
    observed = _run_node(
        r"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync('extension/media-content.js', 'utf8');

        function element(attrs = {}, extra = {}) {
          return {
            getAttribute(name) { return attrs[name] || null; },
            textContent: attrs.textContent || '',
            ...extra,
          };
        }

        function detect(spec) {
          const meta = spec.meta || {};
          const document = {
            title: spec.title || '',
            querySelector(selector) {
              if (selector === 'audio[src], audio source[src]') return spec.audio || null;
              if (selector === 'article, main') return spec.articleRegion || null;
              if (selector.startsWith('meta[')) {
                const value = meta[selector];
                return value == null ? null : element({ content: value });
              }
              return null;
            },
            querySelectorAll(selector) {
              if (selector === 'link[rel~="alternate"][type]') return spec.links || [];
              if (selector === 'script[type="application/ld+json"]') return spec.schemas || [];
              if (selector === 'script') return spec.scripts || spec.schemas || [];
              if (selector === 'iframe[src]') return spec.iframes || [];
              if (selector === 'video') return spec.videos || [];
              return [];
            },
          };
          const parsed = new URL(spec.url);
          const context = vm.createContext({
            URL,
            console,
            document,
            window: { location: {
              href: spec.url,
              hostname: parsed.hostname,
              pathname: parsed.pathname,
            } },
          });
          return vm.runInContext(source, context);
        }

        const article = detect({
          url: 'https://example.com/essay',
          title: 'A long essay about attention',
        });
        const podcast = detect({
          url: 'https://example.com/shows',
          title: 'Example Podcast Episodes',
          links: [element({
            type: 'application/rss+xml',
            href: 'https://feeds.example.com/show.rss',
          })],
        });
        const slugEpisode = detect({
          url: 'https://example.com/shows/episode-7',
          title: 'Example Podcast — Episode 7',
          links: [element({
            type: 'application/rss+xml',
            href: 'https://feeds.example.com/show.rss',
          })],
        });
        const podcastEpisode = detect({
          url: 'https://example.com/shows/episode/7',
          title: 'Example Podcast Episode 7',
          links: [element({
            type: 'application/rss+xml',
            href: 'https://feeds.example.com/show.rss',
          })],
          audio: element({ src: 'https://cdn.example.com/episode-7.mp3' }),
          schemas: [element({
            textContent: '{"@type":"PodcastEpisode","name":"Episode 7"}',
          })],
        });
        const video = detect({
          url: 'https://example.com/watch/keynote',
          title: 'Keynote',
          videos: [element({ src: 'https://cdn.example.com/keynote.mp4?token=abc' }, {
            controls: true,
            getBoundingClientRect() { return { width: 640, height: 360 }; },
          })],
        });
        const embeddedPodcast = detect({
          url: 'https://www.founderspodcast.com/episodes',
          title: 'Founders Podcast',
          schemas: [element({
            textContent: '{"@type":"PodcastSeries","name":"Founders"}',
          })],
          scripts: [
            ...Array.from({ length: 200 }, (_value, index) => element({
              textContent: `loader-${index}`,
            })),
            element({
              textContent: 'self.__next_f.push([1,"audioUrl\\\":\\\"https://traffic.megaphone.fm/latest.mp3\\\""])',
            }),
          ],
        });
        const ambiguousEpisodeHydration = detect({
          url: 'https://example.com/episodes/7',
          title: 'Example Podcast Episode 7',
          schemas: [element({
            textContent: '{"@type":"PodcastEpisode","name":"Episode 7"}',
          })],
          scripts: [element({
            textContent: 'audioUrl:"https://cdn.example.com/latest.mp3" audioUrl:"https://cdn.example.com/episode-7.mp3"',
          })],
        });
        const embed = detect({
          url: 'https://example.com/post-with-video',
          title: 'A video post',
          iframes: [element({ src: 'https://player.vimeo.com/video/123' }, {
            getBoundingClientRect() { return { width: 640, height: 360 }; },
          })],
        });
        const articleWithMedia = detect({
          url: 'https://example.com/essay-with-embeds',
          title: 'A long essay with references',
          audio: element({ src: 'https://cdn.example.com/narration.mp3' }),
          schemas: [element({
            textContent: '{"@type":"Article","name":"Long essay"}',
          })],
          articleRegion: element({ textContent: 'x'.repeat(3000) }, {
            innerText: 'x'.repeat(3000),
          }),
          iframes: [element({ src: 'https://www.youtube.com/embed/abc' }, {
            getBoundingClientRect() { return { width: 640, height: 360 }; },
          })],
        });
        const narratedArticle = detect({
          url: 'https://example.com/narrated-essay',
          title: 'A narrated essay about attention',
          audio: element({ src: 'https://cdn.example.com/narration.mp3' }),
          schemas: [element({
            textContent: '{"@type":"AudioObject","name":"Listen to this article"}',
          })],
        });
        const mixedArticlePodcast = detect({
          url: 'https://example.com/essay-with-podcast',
          title: 'An essay with a podcast reference',
          schemas: [
            element({ textContent: '{"@type":"Article","name":"Essay"}' }),
            element({ textContent: '{"@type":"PodcastEpisode","name":"Embedded episode"}' }),
          ],
          articleRegion: element({ textContent: 'x'.repeat(3000) }, {
            innerText: 'x'.repeat(3000),
          }),
        });
        console.log(JSON.stringify({
          article, podcast, slugEpisode, podcastEpisode, video, embeddedPodcast,
          ambiguousEpisodeHydration, embed, articleWithMedia, narratedArticle,
          mixedArticlePodcast,
        }));
        """
    )

    assert observed["article"] is None
    assert observed["podcast"] == {
        "media_type": "podcast",
        "media_scope": "show",
        "url": "https://feeds.example.com/show.rss",
        "reason": "podcast-feed",
    }
    assert observed["slugEpisode"] == {
        "media_type": "podcast",
        "media_scope": "episode",
        "url": "https://example.com/shows/episode-7",
        "reason": "podcast-page",
    }
    assert observed["podcastEpisode"] == {
        "media_type": "podcast",
        "media_scope": "episode",
        "url": "https://cdn.example.com/episode-7.mp3",
        "reason": "audio-element",
    }
    assert observed["video"]["media_type"] == "video"
    assert observed["video"] == {
        "media_type": "video",
        "url": "https://cdn.example.com/keynote.mp4?token=abc",
        "reason": "video-element",
    }
    assert observed["embeddedPodcast"] == {
        "media_type": "podcast",
        "media_scope": "episode",
        "url": "https://traffic.megaphone.fm/latest.mp3",
        "reason": "embedded-audio",
    }
    assert observed["ambiguousEpisodeHydration"] == {
        "media_type": "podcast",
        "media_scope": "episode",
        "url": "https://example.com/episodes/7",
        "reason": "podcast-page",
    }
    assert observed["embed"]["url"] == "https://player.vimeo.com/video/123"
    assert observed["articleWithMedia"] is None
    assert observed["narratedArticle"] is None
    assert observed["mixedArticlePodcast"] is None


def test_page_link_and_selection_use_the_correct_ingest_contract() -> None:
    observed = _run_node(
        r"""
        const fs = require('fs');
        const vm = require('vm');

        const event = { addListener() {}, removeListener() {} };
        let runtimeMessageListener = null;
        const runtimeMessageEvent = {
          addListener(listener) { runtimeMessageListener = listener; },
          removeListener() {},
        };
        const fetchCalls = [];
        const scriptCalls = [];
        const tabCreates = [];
        const sessionStore = {};
        let phase = '';

        const chrome = {
          runtime: {
            id: 'mastisk-test',
            lastError: null,
            onInstalled: event,
            onStartup: event,
            onMessage: runtimeMessageEvent,
          },
          contextMenus: {
            onClicked: event,
            async removeAll() {},
            create(_properties, callback) { callback?.(); },
          },
          notifications: {
            onClicked: event,
            create(_properties, callback) { callback?.(`notification-${fetchCalls.length}`); },
            clear() {},
          },
          alarms: {
            onAlarm: event,
            async create() {},
            async clear() {},
          },
          action: {
            setBadgeBackgroundColor() {},
            setBadgeText() {},
          },
          storage: {
            sync: {
              async get() { return { settings: { serverUrl: 'http://localhost:5555' } }; },
            },
            session: {
              async get(key) { return { [key]: sessionStore[key] }; },
              async set(values) { Object.assign(sessionStore, values); },
            },
          },
          scripting: {
            async executeScript(spec) {
              const file = spec.files?.[0] || '<function>';
              scriptCalls.push({ phase, file });
              if (file === 'media-content.js') {
                if (phase === 'dom-video-page') {
                  return [{ result: {
                    media_type: 'video',
                    url: 'https://cdn.example.com/talk.mp4',
                    reason: 'video-element',
                  } }];
                }
                if (phase === 'podcast-page' || phase === 'podcast-link') {
                  return [{ result: {
                    media_type: 'podcast',
                    media_scope: 'episode',
                    url: 'https://cdn.example.com/exact-episode.mp3',
                    reason: 'audio-element',
                  } }];
                }
                return [{ result: null }];
              }
              if (file === 'chat-content.js') {
                return [{ result: {
                  type: 'page',
                  title: 'An Essay',
                  author: 'Author',
                  content: '# An Essay\n\nTextual Markdown body.',
                  url: 'https://example.com/essay',
                } }];
              }
              return [{ result: true }];
            },
          },
          tabs: {
            onUpdated: event,
            async create(spec) { tabCreates.push(spec); return { id: 99, ...spec }; },
            async remove() {},
            get(_tabId, callback) {
              const tab = { status: 'complete' };
              callback?.(tab);
              return Promise.resolve(tab);
            },
            async query() { return []; },
          },
        };

        async function fetch(url, options = {}) {
          const body = options.body ? JSON.parse(options.body) : null;
          fetchCalls.push({ phase, url, body });
          if (url.endsWith('/api/listen')) {
            return { ok: true, status: 200, async json() { return { job_id: 77 }; } };
          }
          if (url.endsWith('/api/ingest/web')) {
            return {
              ok: true,
              status: 200,
              async json() { return { article: { id: 'a1', title: 'An Essay' } }; },
            };
          }
          if (url.endsWith('/api/ask')) {
            return {
              ok: true,
              status: 200,
              async json() { return { answer: 'bounded context', cites: [], hits: [] }; },
            };
          }
          throw new Error(`unexpected fetch ${url}`);
        }

        const fastSetTimeout = (fn, delay, ...args) =>
          setTimeout(fn, Math.min(delay, 5), ...args);

        const context = vm.createContext({
          AbortSignal,
          URL,
          chrome,
          console,
          fetch,
          setTimeout: fastSetTimeout,
          clearTimeout,
          queueMicrotask,
        });
        vm.runInContext(fs.readFileSync('extension/background.js', 'utf8'), context);

        (async () => {
          const youtubeTab = {
            id: 1,
            url: 'https://www.youtube.com/watch?v=abc',
            title: 'A YouTube talk',
          };
          const articleTab = {
            id: 2,
            url: 'https://example.com/essay',
            title: 'An Essay',
          };
          const domVideoTab = {
            id: 3,
            url: 'https://events.example.com/talk',
            title: 'A recorded talk',
          };
          const podcastTab = {
            id: 4,
            url: 'https://example.podbean.com/e/exact-episode',
            title: 'Exact podcast episode',
          };

          phase = 'youtube-page';
          await context.sendPage(youtubeTab);
          phase = 'dom-video-page';
          await context.sendPage(domVideoTab);
          phase = 'podcast-page';
          await context.sendPage(podcastTab);
          phase = 'article-page';
          await context.sendPage(articleTab);
          phase = 'selection';
          await context.sendSelection({ selectionText: 'the exact selected text' }, articleTab);
          phase = 'media-selection';
          await context.sendSelection({ selectionText: 'selected YouTube description' }, youtubeTab);
          phase = 'youtube-link';
          await context.sendLink('https://youtu.be/xyz', articleTab);
          phase = 'podcast-link';
          await context.sendLink('https://example.podbean.com/e/exact-episode', articleTab);
          phase = 'ask-long-page';
          const askResponse = await new Promise((resolve, reject) => {
            const keepAlive = runtimeMessageListener({
              action: 'ask',
              question: 'What matters here?',
              page_content: 'x'.repeat(25000),
            }, { id: 'mastisk-test' }, resolve);
            if (keepAlive !== true) reject(new Error('ask listener did not stay alive'));
          });
          await new Promise((resolve) => setTimeout(resolve, 0));

          const classifications = [
            'https://www.youtube.com/watch?v=abc',
            'https://www.youtube.com/channel/example',
            'https://www.youtube.com/playlist?list=PL123',
            'https://cdn.example.com/episode.mp3?token=x',
            'https://example.com/article?next=episode.mp3',
            'https://example.com/article#clip.mp4',
            'https://feeds.example.com/show.rss',
            'https://vimeo.com/123',
            'https://podcasts.apple.com/us/podcast/show/id123',
            'https://example.podbean.com/e/exact-episode',
            'https://evilyoutube.com/watch?v=abc',
            'https://example.com/essay',
          ].map((url) => [url, context.classifyMediaUrl(url)]);
          console.log(JSON.stringify({
            fetchCalls, scriptCalls, tabCreates, classifications, askResponse,
          }));
        })().catch((error) => {
          console.error(error);
          process.exit(2);
        });
        """
    )

    listen_calls = [c for c in observed["fetchCalls"] if c["url"].endswith("/api/listen")]
    web_calls = [c for c in observed["fetchCalls"] if c["url"].endswith("/api/ingest/web")]
    assert [c["phase"] for c in listen_calls] == [
        "youtube-page",
        "dom-video-page",
        "podcast-page",
        "youtube-link",
        "podcast-link",
    ]
    listen_by_phase = {c["phase"]: c for c in listen_calls}
    assert listen_by_phase["youtube-page"]["body"]["media_type"] == "video"
    assert listen_by_phase["dom-video-page"]["body"]["media_type"] == "video"
    assert listen_by_phase["youtube-link"]["body"]["media_type"] == "video"
    for phase in ("podcast-page", "podcast-link"):
        assert listen_by_phase[phase]["body"] == {
            "url": "https://cdn.example.com/exact-episode.mp3",
            "media_type": "podcast",
            "media_scope": "episode",
        }
    assert [c["phase"] for c in web_calls] == [
        "article-page",
        "selection",
        "media-selection",
    ]
    assert web_calls[1]["body"]["selection"] == "the exact selected text"
    assert web_calls[2]["body"]["selection"] == "selected YouTube description"
    assert observed["tabCreates"] == [
        {"url": "https://example.podbean.com/e/exact-episode", "active": False}
    ]
    ask_calls = [c for c in observed["fetchCalls"] if c["url"].endswith("/api/ask")]
    assert len(ask_calls) == 1
    assert len(ask_calls[0]["body"]["page_content"]) == 20_000
    assert observed["askResponse"]["ok"] is True

    chat_phases = {
        c["phase"] for c in observed["scriptCalls"] if c["file"] == "chat-content.js"
    }
    assert chat_phases == {"article-page", "selection", "media-selection"}

    classified = dict(observed["classifications"])
    assert classified["https://www.youtube.com/watch?v=abc"]["media_type"] == "video"
    assert classified["https://www.youtube.com/channel/example"] is None
    assert classified["https://www.youtube.com/playlist?list=PL123"] is None
    assert classified["https://cdn.example.com/episode.mp3?token=x"]["media_type"] == "podcast"
    assert classified["https://example.com/article?next=episode.mp3"] is None
    assert classified["https://example.com/article#clip.mp4"] is None
    assert classified["https://feeds.example.com/show.rss"] is None
    assert classified["https://vimeo.com/123"]["media_type"] == "video"
    assert classified["https://podcasts.apple.com/us/podcast/show/id123"]["media_type"] == "podcast"
    assert classified["https://example.podbean.com/e/exact-episode"]["media_scope"] == "episode"
    assert classified["https://evilyoutube.com/watch?v=abc"] is None
    assert classified["https://example.com/essay"] is None
