# one-offs

A scratchpad of small, self-contained side projects. Each subfolder is its own thing — open the subfolder's `index.html` in a browser, or read its `README.md` if it has one.

## Projects

### [`genre-masher-prompts/`](./genre-masher-prompts/)

Generate absurd film mashup pitches by combining two random sub-genres + a randomly assembled character archetype. Includes:

- A standalone browser game (`index.html`) — click to roll mashups; on-demand AI poster generation via Pollinations.ai.
- A batch generator (`generate_mashups.py`) that drives a local llama.cpp server + ComfyUI to produce a CSV, a static gallery (`mashups.html`), and 1280×1664 poster PNGs for each pitch.

See the [project README](./genre-masher-prompts/README.md) for setup and usage.

### [`random-walk/`](./random-walk/)

A single-page interactive visualization of a 2D random walk on a grid, rendered as a heatmap. Adjustable speed, grid size, fade rate, and step budget. No server, no dependencies — just open `index.html`.

## Top-level `index.html`

The root [`index.html`](./index.html) is a simple landing page with cards linking to each subproject. Open it directly in a browser for a quick launcher.
