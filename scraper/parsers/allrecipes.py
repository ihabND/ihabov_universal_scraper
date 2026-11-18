from . import jsonld, parser

parser(
    "allrecipes.com",
    r"/recipe/\d+/[^/]+/?|/[^/]+-recipe-\d+/?",
    "https://www.allrecipes.com/recipe/10813/best-chocolate-chip-cookies/",
    label="Recipe page",
    pages="A recipe page, like /recipe/10813/best-chocolate-chip-cookies/ "
    "or /apple-cider-bath-cinnamon-rolls-recipe-12111965",
    fields="name, description, author, rating, rating_count, prep_minutes, cook_minutes, total_minutes, yield, "
    "categories, cuisine, ingredients, instructions, nutrition, image, date_published",
    json_ld_only=True,
)(jsonld.recipe)
