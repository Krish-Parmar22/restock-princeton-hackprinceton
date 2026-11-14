# E2E test image sources

Fetched on demand by `tests/e2e/fetch_fixtures.py` into `tests/e2e/.cache/` (gitignored), so no images are committed.

| Cache file | Source | Author | License |
|---|---|---|---|
| `scene_toppings.jpg` | https://www.flickr.com/photos/33756523@N05/3463281604 ("prepped and ready") | bagaball | CC BY 2.0 |
| `hand_overhead.jpg` | https://commons.wikimedia.org/wiki/File:Arthritis_on_the_Right_Hand.jpg | CDC, via Wikimedia Commons | CC0 |

`tests/e2e/scene.py` composites them into grab packages:
- the hand is pasted reaching into a bin for the DURING frames;
- for AFTER, part of the slice stack is covered with board texture.

Bin boxes (zones) were drawn by hand from the scene photo.
