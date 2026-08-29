"""The default solution board: fixes waiting on The Fusion Studio website.

Edit this file to change what a guest sees before the host writes anything.
Bump ``SEED_VERSION`` and the desk retires the previous defaults on next
start — briefs the host wrote by hand are never touched.

Each brief carries an acceptance ``spec``. The spec is graded blind (the
suite author never sees the solution) inside a container with **no network**,
so every requirement here is phrased as something a test can read off the
repository's own source files.
"""

from __future__ import annotations

SEED_VERSION = "fusionstudio-1"

#: Repo the default board works on. Cloned on first claim if the folder is gone.
SEED_PROJECT = {
    "slug": "thefusionstudio",
    "name": "The Fusion Studio site",
    "github": "https://github.com/boscojacinto/thefusionstudio",
}

#: What the rolling ticker should advertise. Change this list, bump the
#: version, and the brief below follows.
SERVICES = [
    "Korean spa",
    "Nail spa",
    "Hair studio",
    "Skin & facial",
    "Bridal makeup",
    "Waxing & threading",
    "Garden cafe",
]

_SERVICE_LINE = ", ".join(SERVICES)

_NO_NETWORK = (
    "The grader runs offline with no `npm install`, so every check must read "
    "the repository's own files (source, `public/`, and the `scripts/` "
    "generators) rather than build or serve the site."
)

#: (id, title, brief, wellness_minutes, break_after, spec)
SEED_BOARD = [
    {
        "id": "seed-no-tattoo",
        "title": "Take tattoo off The Fusion Studio site",
        "brief": (
            "The studio no longer advertises tattoo work. Remove it from the site "
            "completely — copy, navigation, the section itself, photos, alt text, "
            "and the SEO metadata — and leave salon and cafe reading as a whole "
            "site rather than one with a hole in it."
        ),
        "wellness_minutes": 90,
        "break_after": 50,
        "spec": (
            "The site must not mention or show tattoo work anywhere.\n\n"
            "- No file under `app/`, `components/`, `lib/`, or `scripts/` contains "
            "the word \"tattoo\" in any case, and none contains the Devanagari "
            "टॅटू.\n"
            "- `lib/site.ts` signage, tagline, promise, and description name only the "
            "salon and the cafe.\n"
            "- The nav list in `lib/site.ts` has no `#tattoo` entry, and no component "
            "renders a section with `id=\"tattoo\"`.\n"
            "- No gallery entry in `lib/site.ts` has alt text describing tattoo or ink "
            "work, and every image path a gallery entry names still exists under "
            "`public/`.\n"
            "- The tattoo photographs are deleted from `public/photos/`, and no file "
            "left in the repository references a deleted path.\n"
            "- The keyword list and page title in `app/layout.tsx` no longer sell "
            "tattoo, and `README.md` describes the site without it.\n"
            "- The `scripts/write_*.py` generators are updated too, so re-running them "
            "cannot put tattoo back.\n\n"
            f"{_NO_NETWORK}"
        ),
    },
    {
        "id": "seed-morning-cafe",
        "title": "Make the cafe a morning cafe, not an after-dark one",
        "brief": (
            "The cafe opens in the morning: coffee, breakfast, daylight in the "
            "garden. Today the site sells night neon and \"after dark\". Re-frame "
            "the cafe — copy, headings, hero and section imagery — around morning "
            "without flattening the studio's voice."
        ),
        "wellness_minutes": 90,
        "break_after": 50,
        "spec": (
            "The cafe must read as a morning cafe everywhere it appears.\n\n"
            "- The cafe entry in `components/Paths.tsx` sells the morning — coffee, "
            "breakfast, daylight — and its copy and note carry no night, neon, "
            "after-dark, or late framing.\n"
            "- `site.tagline` in `lib/site.ts` no longer sells night neon, and neither "
            "`tagline`, `promise`, nor `description` describes the venue as a night "
            "or after-dark place.\n"
            "- The `Visit` heading no longer reads \"after dark or after coffee\", and "
            "no heading on the page invites guests after dark.\n"
            "- `app/page.tsx` renders no night-sign section, and the night photographs "
            "it used are replaced by daylight ones in both the page and the gallery "
            "alt text.\n"
            "- Somewhere on the page a guest can read that the cafe opens in the "
            "morning.\n"
            "- The `scripts/write_*.py` generators produce the morning copy as well.\n\n"
            f"{_NO_NETWORK}"
        ),
    },
    {
        "id": "seed-services-ticker",
        "title": "Turn the rolling ticker into the service list",
        "brief": (
            "The ticker under the hero rolls brand words. It should roll the "
            "services instead, so a guest scrolling past learns what the studio "
            "actually does: "
            f"{_SERVICE_LINE}. Keep it one seamless loop, and keep the list in one "
            "place so it can be edited without touching the animation."
        ),
        "wellness_minutes": 90,
        "break_after": 50,
        "spec": (
            "The rolling ticker must advertise the studio's services.\n\n"
            f"- `lib/site.ts` exports a single list of services: {_SERVICE_LINE}.\n"
            "- `components/Marquee.tsx` renders its items from that exported list and "
            "hardcodes no service name of its own.\n"
            "- Every service in the list appears in the rendered ticker, in the order "
            "the list gives.\n"
            "- The ticker shows no word that is not a service — in particular no "
            "\"tattoo\".\n"
            "- The track still repeats the list often enough to fill the row, so the "
            "loop has no visible gap.\n"
            "- The repeated copies are hidden from assistive technology, so a screen "
            "reader hears each service once.\n"
            "- `scripts/write_*.py` generates the same list.\n\n"
            f"{_NO_NETWORK}"
        ),
    },
]

#: Board titles this file has retired. Kept so an old salon.db can be cleaned up.
LEGACY_TITLES = [
    "Fix the PeriPage QR slip so it scans in low cafe light",
    "Join guide that a first-time Android guest can follow on cafe Wi-Fi",
    "Wellness break chime that cannot be skipped from the seat",
]
