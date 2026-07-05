from __future__ import annotations


def test_static_x_article_card_extracts_structured_twitter_source():
    from mastisk.integrations import twitter

    html = """
    <html>
      <head>
        <title>Thariq on X: &quot;https://t.co/hPiZr1kG7r&quot; / X</title>
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "SocialMediaPosting",
            "articleBody": "https://t.co/hPiZr1kG7r",
            "author": {
              "@type": "Person",
              "alternateName": "@trq212",
              "name": "Thariq",
              "url": "https://x.com/trq212"
            },
            "datePublished": "2026-07-03T17:43:35.000Z",
            "identifier": "2073100352921215386",
            "interactionStatistic": [
              {"@type": "InteractionCounter", "name": "Likes", "userInteractionCount": 7557},
              {"@type": "InteractionCounter", "name": "Replies", "userInteractionCount": 222}
            ],
            "sharedContent": [{"@type": "WebPage", "url": "http://x.com/i/article/2073090223194755072"}],
            "text": "https://t.co/hPiZr1kG7r",
            "url": "https://x.com/trq212/status/2073100352921215386"
          }
        </script>
      </head>
      <body>
        <article data-tweet-id="2073100352921215386">
          <a href="/i/article/2073090223194755072">
            <img alt="Article cover image" src="https://pbs.twimg.com/media/HMUY_HnbcAAa51I.jpg" />
            <div aria-label="Article">Article</div>
            <div>A Field Guide to Fable: Finding Your Unknowns</div>
            <div>Working with Claude Fable 5 keeps re-teaching me an old lesson.</div>
          </a>
        </article>
      </body>
    </html>
    """

    data = twitter._article_data_from_static_html(
        "https://x.com/trq212/status/2073100352921215386",
        html,
    )

    assert data is not None
    assert data.url == "https://x.com/trq212/status/2073100352921215386"
    assert data.author == "Thariq"
    assert data.published_at == "2026-07-03T17:43:35.000Z"
    assert data.title == "Thariq on X: A Field Guide to Fable: Finding Your Unknowns"
    assert "X post by Thariq (@trq212)" in data.text
    assert "Tweet:\nhttps://t.co/hPiZr1kG7r" in data.text
    assert "Shared link:" in data.text
    assert "Title: A Field Guide to Fable: Finding Your Unknowns" in data.text
    assert "Summary: Working with Claude Fable 5 keeps re-teaching me an old lesson." in data.text
    assert "URL: https://x.com/i/article/2073090223194755072" in data.text
    assert "- Likes: 7557" in data.text
    assert data.hero_image_url == "https://pbs.twimg.com/media/HMUY_HnbcAAa51I.jpg"
    assert data.inline_media == [
        {
            "src": "https://pbs.twimg.com/media/HMUY_HnbcAAa51I.jpg",
            "alt": "Article cover image",
        }
    ]


def test_twitter_refresh_replaces_old_collapsed_raw_text():
    from mastisk.integrations import twitter
    from mastisk.integrations.article import ArticleData

    old = """# Thariq on X: &quot;https://t.co/hPiZr1kG7r&quot; / X

https://x.com/trq212/status/2073100352921215386

https://t.co/hPiZr1kG7r
Thariq@trq212ArticleA Field Guide to Fable: Finding Your UnknownsWorking with Claude Fable 5...
"""
    new = ArticleData(
        url="https://x.com/trq212/status/2073100352921215386",
        title="Thariq on X: A Field Guide to Fable: Finding Your Unknowns",
        text=(
            "X post by Thariq (@trq212)\n"
            "URL: https://x.com/trq212/status/2073100352921215386\n\n"
            "Tweet:\nhttps://t.co/hPiZr1kG7r\n\n"
            "Shared link:\nTitle: A Field Guide to Fable: Finding Your Unknowns\n"
            "Summary: Working with Claude Fable 5 keeps re-teaching me an old lesson."
        ),
    )

    assert twitter.needs_refresh(old, new) is True


def test_twitter_refresh_does_not_replace_collapsed_card_with_url_only_oembed():
    from mastisk.integrations import twitter
    from mastisk.integrations.article import ArticleData

    old = """# Thariq on X: &quot;https://t.co/hPiZr1kG7r&quot; / X

https://x.com/trq212/status/2073100352921215386

https://t.co/hPiZr1kG7r
Thariq@trq212ArticleA Field Guide to Fable: Finding Your UnknownsWorking with Claude Fable 5 keeps re-teaching me an old lesson: the map is not the territory.
"""
    new = ArticleData(
        url="https://x.com/trq212/status/2073100352921215386",
        title="Thariq on X: https://t.co/hPiZr1kG7r",
        text=(
            "X post by Thariq (@trq212)\n"
            "URL: https://x.com/trq212/status/2073100352921215386\n\n"
            "Tweet:\nhttps://t.co/hPiZr1kG7r"
        ),
    )

    assert twitter.needs_refresh(old, new) is False
