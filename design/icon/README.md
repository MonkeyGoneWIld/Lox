# The Lox mark

Four bars off a baseline: the spectrogram, which is the screen this app is
really about and the one you read on every upload.

`marks.py` holds the geometry — the mark, and the four candidates it beat, kept
because a mark is chosen by looking at it beside the others. `build.py` turns it
into every asset the app serves:

```
python design/icon/build.py
```

Tab art is transparent, so it sits on whatever colour the browser is. The iOS
and Windows tiles get the app's own `--bg`, because neither platform honours
transparency there and both would otherwise pick a ground for you.

The mark it replaced was a Flaticon fish that came with smoked-salmon, on an
opaque pale-blue square, under a CC-BY credit carried in the page footer. Both
are gone.
