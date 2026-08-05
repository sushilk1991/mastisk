// ============================================================
// Mastisk Clipper — Lightweight primary-media detector
// ============================================================
// Injected before Defuddle when the user clips a page or link. Returns either
// { media_type, media_scope?, url, reason } or null. Detection is kept
// deliberately conservative: an incidental tracking/ad video must not turn an
// article clip into a transcription job.

(() => {
  try {
    const pageUrl = window.location.href;
    const parsedPageUrl = new URL(pageUrl);
    const host = window.location.hostname.toLowerCase().replace(/^www\./, '');

    function httpUrl(value) {
      if (typeof value !== 'string' || !value.trim()) return '';
      try {
        const parsed = new URL(value, pageUrl);
        return /^https?:$/.test(parsed.protocol) ? parsed.href : '';
      } catch {
        return '';
      }
    }

    function metaContent(selector) {
      return document.querySelector(selector)?.getAttribute('content') || '';
    }

    function hostMatches(candidate, domain) {
      return candidate === domain || candidate.endsWith(`.${domain}`);
    }

    const rssLink = Array.from(
      document.querySelectorAll('link[rel~="alternate"][type]'),
    ).find((el) => /application\/(rss|atom)\+xml/i.test(el.getAttribute('type') || ''));
    const rssUrl = httpUrl(rssLink?.getAttribute('href') || '');

    const audioEl = document.querySelector('audio[src], audio source[src]');
    const audioUrl = httpUrl(
      audioEl?.currentSrc || audioEl?.src || audioEl?.getAttribute?.('src') || '',
    );

    const schemaScripts = Array.from(
      document.querySelectorAll('script[type="application/ld+json"]'),
    ).slice(0, 20);
    const schemaText = schemaScripts
      .map((el) => (el.textContent || '').slice(0, 100000))
      .join('\n');
    const hasPodcastSchema = /["']@type["']\s*:\s*["'](?:PodcastEpisode|PodcastSeries)["']/i
      .test(schemaText);
    const hasPodcastSeriesSchema = /["']@type["']\s*:\s*["']PodcastSeries["']/i
      .test(schemaText);
    const hasAudioSchema = /["']@type["']\s*:\s*["']AudioObject["']/i.test(schemaText);
    const hasPodcastEpisodeSchema = /["']@type["']\s*:\s*["']PodcastEpisode["']/i
      .test(schemaText);
    const hasVideoSchema = /["']@type["']\s*:\s*["']VideoObject["']/i.test(schemaText);
    const hasArticleSchema = /["']@type["']\s*:\s*["'](?:Article|NewsArticle|BlogPosting)["']/i
      .test(schemaText);

    // Some podcast sites (including Founders Podcast) expose the latest
    // enclosure only in framework hydration data. yt-dlp cannot necessarily
    // ingest the surrounding show page, but the Listener can ingest the
    // enclosure directly. Bound the scan so a pathological page cannot make a
    // context-menu click churn through an unlimited script payload.
    function audioUrlFromScripts(scripts, byteLimit = 1_500_000) {
      let remaining = byteLimit;
      for (const script of scripts) {
        if (remaining <= 0) break;
        const raw = script.textContent || '';
        if (!/(?:audioUrl|contentUrl|enclosureUrl)/i.test(raw)) continue;
        const chunk = raw.slice(0, remaining)
          .replace(/\\u0026/gi, '&')
          .replace(/\\\//g, '/');
        remaining -= chunk.length;
        const candidates = chunk.match(/https?:\/\/[^\s"'\\<>]+/gi) || [];
        const found = candidates.find((value) => /\.(?:mp3|m4a|ogg|opus|wav|aac|flac)(?:$|[?#])/i.test(value));
        if (found) return httpUrl(found);
      }
      return '';
    }

    function embeddedAudioUrl() {
      const allScripts = Array.from(document.querySelectorAll('script'));
      // Structured data tends to be near <head>; framework hydration payloads
      // (notably Next.js) are emitted at the very end. Sample both ends instead
      // of letting thousands of tiny loader scripts hide the useful payload.
      const scripts = allScripts.length > 160
        ? [...allScripts.slice(0, 80), ...allScripts.slice(-80)]
        : allScripts;
      return audioUrlFromScripts(scripts);
    }

    const ogType = metaContent('meta[property="og:type"]').toLowerCase();
    const descriptor = [
      document.title,
      metaContent('meta[name="description"]'),
      metaContent('meta[property="og:title"]'),
      metaContent('meta[property="og:description"]'),
    ].join(' ');
    const describesPodcast = /\b(podcasts?|episodes?)\b/i.test(descriptor);
    const structuredPodcast = hasPodcastSchema || (hasAudioSchema && describesPodcast);
    const pathSuggestsEpisode = /\/(?:episodes?|e)\/[^/]+|\/[^/]*episode[-_][^/]+/i
      .test(window.location.pathname);
    const titleSuggestsEpisode = /\bepisode\s*(?:#?\d+|[-:#–—]\s*\S+)/i
      .test(document.title);
    const episodeLocation = pathSuggestsEpisode
      || titleSuggestsEpisode
      || parsedPageUrl.searchParams.has('i');
    const episodePage = hasPodcastEpisodeSchema || episodeLocation;
    const structuredEpisodeAudio = hasPodcastEpisodeSchema
      ? audioUrlFromScripts(schemaScripts, 300000)
      : '';
    // A show/list page can safely use its first hydration enclosure as the
    // latest episode. An episode page cannot: its payload may contain the
    // entire show, so only episode-scoped JSON-LD or the live player is exact.
    const embeddedAudio = structuredPodcast && !episodePage ? embeddedAudioUrl() : '';
    const podcastHost = /(^|\.)(podcasts\.apple\.com|podcasts\.google\.com|pca\.st|podbean\.com|buzzsprout\.com|simplecast\.com|transistor\.fm|captivate\.fm|spreaker\.com|libsyn\.com|megaphone\.fm|overcast\.fm|castbox\.fm)$/.test(host)
      || (host === 'open.spotify.com' && /^\/(episode|show)\//.test(window.location.pathname));
    const articleRegion = document.querySelector('article, main');
    const articleTextLength = (articleRegion?.innerText || articleRegion?.textContent || '')
      .trim().length;
    const substantialArticle = hasArticleSchema || articleTextLength >= 1600;
    const podcastIdentity = podcastHost || ogType.includes('podcast');
    const podcastSignal = structuredPodcast
      || (!!audioEl && describesPodcast)
      || (!!rssUrl && describesPodcast);
    const podcastPage = podcastIdentity
      || hasPodcastSeriesSchema
      || (!substantialArticle && podcastSignal)
      || (!hasArticleSchema && episodeLocation && hasPodcastEpisodeSchema);

    if (podcastPage) {
      const targetUrl = audioUrl
        || structuredEpisodeAudio
        || embeddedAudio
        || (episodePage ? pageUrl : (rssUrl || pageUrl));
      return {
        media_type: 'podcast',
        media_scope: audioUrl || structuredEpisodeAudio || embeddedAudio || episodePage
          ? 'episode'
          : 'show',
        url: targetUrl,
        reason: audioUrl
          ? 'audio-element'
          : (structuredEpisodeAudio
            ? 'structured-audio'
            : (embeddedAudio
              ? 'embedded-audio'
              : (!episodePage && rssUrl ? 'podcast-feed' : 'podcast-page'))),
      };
    }

    const videoFrame = Array.from(document.querySelectorAll('iframe[src]')).find((el) => {
      const src = httpUrl(el.getAttribute('src') || '');
      if (!src) return false;
      try {
        const frameHost = new URL(src).hostname.toLowerCase();
        const knownVideoHost = hostMatches(frameHost, 'youtube.com')
          || hostMatches(frameHost, 'youtube-nocookie.com')
          || frameHost === 'youtu.be'
          || hostMatches(frameHost, 'vimeo.com')
          || hostMatches(frameHost, 'loom.com')
          || hostMatches(frameHost, 'dailymotion.com')
          || hostMatches(frameHost, 'wistia.com')
          || hostMatches(frameHost, 'wistia.net');
        if (!knownVideoHost) return false;
        const rect = typeof el.getBoundingClientRect === 'function'
          ? el.getBoundingClientRect()
          : { width: 0, height: 0 };
        const width = rect.width || Number(el.getAttribute('width')) || 0;
        const height = rect.height || Number(el.getAttribute('height')) || 0;
        return width >= 320 && height >= 180;
      } catch {
        return false;
      }
    });
    const frameUrl = httpUrl(videoFrame?.getAttribute('src') || '');

    const primaryVideo = Array.from(document.querySelectorAll('video')).find((el) => {
      const rect = typeof el.getBoundingClientRect === 'function'
        ? el.getBoundingClientRect()
        : { width: 0, height: 0 };
      const width = rect.width || Number(el.getAttribute('width')) || 0;
      const height = rect.height || Number(el.getAttribute('height')) || 0;
      return width >= 320 && height >= 180;
    });
    const nestedVideoSource = primaryVideo?.querySelector?.('source[src]');
    const videoElementUrl = httpUrl(
      primaryVideo?.currentSrc
        || primaryVideo?.src
        || primaryVideo?.getAttribute?.('src')
        || nestedVideoSource?.getAttribute?.('src')
        || '',
    );
    const ogVideoUrl = httpUrl(
      metaContent('meta[property="og:video"], meta[property="og:video:url"]'),
    );
    const pageClaimsVideo = hasVideoSchema || ogType.startsWith('video') || !!ogVideoUrl;
    const videoPage = !substantialArticle
      && (!!videoFrame || !!primaryVideo || pageClaimsVideo);

    if (videoPage) {
      return {
        media_type: 'video',
        // Known embeds are a stronger yt-dlp target than the surrounding article.
        // A primary native video's resolved HTTP URL is stronger still for sites
        // whose surrounding page is not supported by yt-dlp. blob: URLs are
        // rejected by httpUrl and therefore fall back to the stable page URL.
        url: frameUrl || videoElementUrl || ogVideoUrl || pageUrl,
        reason: frameUrl
          ? 'video-embed'
          : (videoElementUrl ? 'video-element' : (ogVideoUrl ? 'video-meta' : 'video-page')),
      };
    }
  } catch (err) {
    console.warn('[Mastisk] Media detection failed:', err.message);
  }
  return null;
})();
