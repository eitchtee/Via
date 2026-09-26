# Via branding

The Via mark and every icon generated from it. **Edit only `source/`**, then regenerate:

```sh
uv run --no-project --with playwright python branding/build.py   # needs Google Chrome
```

This rewrites `svg/`, `png/` and the web UI's icons in `src/via/web/`.

## Colors

| | |
|---|---|
| Brand teal (tile background, theme color) | `#0e7c66` |
| Mark on teal | `#ffffff` |
| Light-mode UI accent / dark-mode UI accent | `#0e7c66` / `#3dbd9c` |

## Why variants

The source files draw the mark edge to edge on a square canvas, with no padding. Scaled as-is
it would be clipped by rounded corners, circles and platform masks, and blur at small sizes.
Each variant places the mark on its own canvas, sized for its use:

| File | Looks like | Use it for |
|---|---|---|
| `svg/app-icon.svg`, `png/app-icon-{64…1024}.png` | teal rounded tile, transparent corners | browser extension (store, 128 px), desktop apps, docs, anything showing the icon as-is |
| `svg/app-icon-small.svg`, `png/app-icon-small-{16,32,48}.png` | same, bigger and heavier mark | favicons, extension toolbar (16/32/48 px) |
| `svg/app-icon-square.svg`, `png/app-icon-square-{180,1024}.png` | opaque full-bleed square | iOS app icon, App Store / Play Store listing, `apple-touch-icon` (platform rounds it) |
| `svg/maskable.svg`, `png/maskable-512.png` | full bleed, mark in the 80% safe circle | PWA `"purpose": "maskable"` |
| `svg/android-foreground.svg`, `png/android-foreground-{432,1024}.png` | white mark, transparent, inside the 61% safe circle | Android adaptive icon foreground (background: solid `#0e7c66`) |
| `svg/android-monochrome.svg`, `png/android-monochrome-{432,1024}.png` | black mark, transparent, same geometry | Android 13+ themed icon (only alpha is used) |
| `svg/mark-white.svg`, `png/mark-white-{24,36,48,72,96,1024}.png` | white mark, transparent, small margin | Android notification small icon: 24/36/48/72/96 px = `drawable-{m,h,xh,xxh,xxxh}dpi`; marks on dark surfaces |
| `svg/mark-black.svg`, `png/mark-black-1024.png` | black mark, transparent, small margin | marks on light surfaces, Safari pinned tab |
| `svg/mark.svg` | mark cropped tight, `currentColor` | inline in UI next to text (inherits the text color) |

Safe zones: Android adaptive icons render a 108 dp canvas through a mask; only the central
66 dp circle is guaranteed visible, so the foreground mark fits inside a 61% circle. PWA
maskable icons guarantee a centered 80% circle.

The web UI (`src/via/web/`) uses `favicon.svg` (small tile), `icon.svg`, `icon-192.png`,
`icon-512.png`, `maskable-512.png` and `apple-touch-icon.png`, listed in `manifest.json`.

## Projects that copy these files

After regenerating, update the copies in the sibling projects:

- **ViaApp** (Flutter, Android + Windows): run `scripts/update_icons.py` there. It re-copies
  from `../Via/branding/png` and re-packs `app_icon.ico` and the tray icon.
- **ViaBrowserExtension**: copy `png/app-icon-small-{16,32,48}.png` and `png/app-icon-128.png`
  to `src/icons/icon-{16,32,48,128}.png`.
