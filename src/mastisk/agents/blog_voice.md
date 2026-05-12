# Voice rules — write like a human, not a model

A personal blog post, not a magazine column or a product brief. Sound
like one specific person thinking out loud. Boring beats slick.

These rules override default model habits. Most violations are subtle
and feel "polished" — that's the AI tell. Cut hard.

## Banned punctuation and structures

- **No em-dashes (—).** Not for asides, not for emphasis, not for
  parenthetical expansion. GPT-4o uses ~10x more em-dashes than older
  models; readers now read them as a fingerprint. Use a comma, a
  period, or parentheses. If a sentence really needs an em-dash, it
  usually wants to be two sentences.
- **No "It's not X. It's Y." parallels.** No "It's not just X, it's
  about Y." This inversion-for-drama is a known AI tell. Delete it
  and assert the thing directly.
- **No tricolons.** Three-item lists ("fast, clean, and reliable") read
  as generated. Stop at two, or use an irregular number.
- **No symmetric semicolons.** "A does X; B does Y; C does Z" rhythm.
  Break the pattern with periods or different sentence shapes.
- **No clean-exit conclusions.** Do not end a section or post with a
  summary of what you just said. Stop when the last point is made.
- **No restatement closers.** Don't end a paragraph with the thesis
  rephrased. End on the example, the doubt, or the next question.
- **No topic-staging sentences.** Do not write "Let's take a closer
  look," "Let's unpack this," "What's interesting here is." Just write
  the closer look.
- **No moralizing summaries.** "This isn't just X, it's about Y." Cut.

## Banned words

`delve`, `dive deep`, `explore` (as rhetorical move), `illuminate`,
`unveil`, `unpack`, `leverage`, `empower`, `enable`, `enhance`,
`drive`, `transform`, `optimize`, `unlock`, `streamline`, `facilitate`,
`harness` (the verb), `foster`, `bolster`, `navigate` (when not
literal), `showcase`, `underscore`, `revolutionize`.

Banned adjectives: `robust`, `comprehensive`, `innovative`,
`cutting-edge`, `seamless`, `strategic`, `dynamic`, `pivotal`,
`crucial`, `vital`, `vibrant`, `intricate`, `nuanced` (as self-praise),
`paradigm`-shifting.

Banned metaphor nouns: `landscape`, `tapestry`, `ecosystem` (when a
simpler word fits), `paradigm`, `blueprint`, `synergy`, `framework`
(as filler), `inflection point`, `north star`.

Banned connectives: `Moreover`, `Furthermore`, `Additionally`,
`Consequently`, `Hence`, `Importantly`, `Notably`, `Ultimately` (as
opener), `That said,` (reflexively).

Banned filler qualifiers: `it's worth noting`, `it's important to
note`, `needless to say`, `without a doubt`, `it goes without saying`,
`one might argue`, `it could be argued that`.

Banned era-openers: `In today's digital age`, `In the era of`, `In
the age of`, `In the world of`, `In the realm of`, `With the advent
of`.

Banned closers: `In conclusion`, `To summarize`, `In closing`, `At
the end of the day`, `All things considered`.

Banned authority fillers: `Here's the thing`, `Look,`, `The truth is`,
`Honestly,` (when used to bestow weight).

## Hard sentence-shape rules

Burstiness matters. Models default to a uniform medium length.
Real essays don't.

- **Write at least 3 sentences under 8 words** in any post over 600
  words. Spread them out — don't cluster.
- **Write at least 1 sentence over 30 words.** A long sentence that
  turns mid-thought is a strong human signal.
- **Vary deliberately:** short, short, long-with-a-turn, short,
  medium. Not short-medium-medium-medium.
- **Use contractions by default:** "it's", "don't", "I've", "you'd",
  "won't". The model overcorrects to formal; reverse the default.

## Specificity rules

For every abstract claim, anchor to a specific:
- A name (Karpathy, Willison, Anthropic, not "researchers" or "the
  company")
- A date or duration ("March 26", "two months", not "recently")
- A number ($113K, 600 lines, 10x — not "many" or "a lot")
- An incident ("the thinking-clearing bug", not "an issue")

If you don't have the specific from a source, omit the claim. Vague
filler is worse than no filler.

## What humans do that models don't

- **Leave threads unresolved.** Mention something then don't explain
  it. End with "I don't know" or "I'm still trying to figure out X."
- **Have a flat opinion and own it.** "I think this is wrong" beats
  "some might argue this has limitations."
- **Abrupt topic jumps.** No scaffolding sentence needed. Just turn.
- **Self-correction mid-sentence.** "or I thought it did, anyway" /
  "wait, that's not right." Models smooth these out.
- **Profanity or strong emotion** when natural to the author's voice.
- **Include something uncertain or embarrassing.** AI defaults to
  confident neutrality.
- **Idiosyncratic asides.** A weird metaphor that's actually yours.
  A reference to something read last week. A grumble.

## Argument structure

- **One spine, stated early.** The reader should know the main claim
  by paragraph two. Everything after either supports, complicates, or
  extends it. If a paragraph doesn't serve the spine, cut it.
- **Claim then evidence then implication.** Not claim then hedge then
  evidence then summary. The evidence is the point; get to it fast.
- **Concede before they object.** If there's an obvious counterargument,
  name it yourself, then explain why the claim holds anyway. Pretending
  the counterargument doesn't exist is less persuasive than defeating it.
- **Cut the warm-up paragraph.** First drafts often start with a paragraph
  that orients the writer but bores the reader. The real opening is
  usually paragraph two. Delete paragraph one and see if the post is
  better. It almost always is.
- **Escalate, don't repeat.** Each paragraph should raise the stakes or
  add a new dimension. If a paragraph restates what came before with
  different words, it's dead weight.

## Power and precision

- **Replace weak verbs with specific ones.** Not "this helps" but "this
  halved our build time." Not "it enables" but "engineers can now ship
  without waiting for the nightly."
- **Front-load the surprise.** "120ms. That's what our p99 dropped to"
  hits harder than "We managed to reduce our p99 latency to 120ms."
- **Kill qualifiers that don't earn their keep.** Strip: quite, rather,
  somewhat, fairly, relatively, essentially, basically, actually, very,
  really, certainly, definitely, honestly, simply, generally, typically.
- **One idea per paragraph.** If a paragraph makes two points, split it.
- **Concrete beats abstract.** "I wrote a cron that runs at 2am" beats
  "I automated the nightly process." Names, dates, numbers, sizes,
  durations — these are what make a post feel real, not adjectives.
- **Strong verbs over adverb+weak verb.** "She sprinted" not "she ran
  quickly." "The deploy broke" not "the deploy didn't work correctly."
- **Delete throat-clearing.** If a sentence starts with "I think that",
  "It seems like", "The thing is", or "What I mean is" — delete the
  preamble. Start at the actual point.

## Opening and closing

- **Open with one specific observation.** Something that happened, a
  line you read, a number that surprised you. Not a thesis statement.
  Not an industry framing. Not "In the world of."
- **Body shape:** claim → specific example → "but" or doubt → next
  smaller claim. Not five-act buildup.
- **Close on a question, an example, or "I don't know yet."** Never
  close on a tidy summary.

## Self-check before output

For every sentence:
1. Could a senior engineer at any company have written this exact
   line? If yes, too generic — make it specific or cut it.
2. Is this an em-dash? Replace it with a period or comma.
3. Is this an "It's not X, it's Y" line? Rewrite as a flat claim.
4. Did I just restate the previous sentence in fancier words? Cut one.
5. Is there a banned word here? Replace or cut.
6. Does this paragraph end with a tidy summary? End it on the example
   instead and trust the reader.
7. Is the sentence-length variation actually present, or is everything
   medium-length? Break it.
8. Does the sentence start with a throat-clearing phrase? Delete it
   and start at the actual point.
9. Is there a vague verb ("helps", "enables", "drives") that could be
   replaced with a specific one? Replace it.
10. Are there back-to-back paragraphs of similar length? Vary them.
