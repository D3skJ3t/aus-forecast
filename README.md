# Australian Forecasts

Weather for any place in Australia, with a full side-by-side comparison of forecast sites for a set
of featured locations.

- Site: https://d3skj3t.github.io/aus-forecast
- **Any place:** search a suburb, town or postcode. Shows the Bureau of Meteorology forecast and the
  nearest station's latest reading, fetched live in the browser from the Bureau's location feed. Star a
  place to keep it in "My places" on that device. Link to one with `?place=<geohash>`.
- **Boating conditions** for the chosen day: a calm / choppy / rough guide from the strongest daytime wind, the Bureau's hourly wind and gusts and its warnings for the area (any place), plus Windy's daily wind, and WillyWeather tides and swell for featured coastal locations.
- **Featured locations** (`?loc=<id>`, grouped by region, including jet ski lakes): the full comparison of the Bureau, Weatherzone, WillyWeather,
  Elders Weather, Weather Underground and Windy. The location list and scraped figures are read from
  [oxley-forecast-public](https://github.com/D3skJ3t/oxley-forecast-public) (`locations.json` and
  `data/<id>.json`), whose scraper collects them; this repo has no scraper of its own.

A single static page (`index.html`) served by GitHub Pages. Figures belong to the sites they come from;
for warnings and safety decisions, follow the Bureau of Meteorology.
