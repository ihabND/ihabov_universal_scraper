from . import jsonld, parser

parser(
    "imdb.com",
    r"/title/tt\d+/?",
    "https://www.imdb.com/title/tt0111161/",
    label="Title page, uses the browser",
    pages="A title page, like /title/tt0111161/ (movies, series and episodes). "
    "Needs the browser, which browser=auto uses automatically.",
    fields="title, alternate_title, type, year, date_published, content_rating, genres, rating, rating_count, "
    "runtime_minutes, directors, writers, actors, keywords, description, poster, trailer, url",
    json_ld_only=True,
    min_tier="browser",
)(jsonld.movie)
