import json
import os
import re
import requests
import urllib.request
from bs4 import BeautifulSoup
from pypersist import persist


def get_bird_dictionary(domain, prefix, exceptions, uncommon_threshold, common_threshold):
    bird_data = scrape_all_data(domain, prefix)
    apply_exceptions(bird_data, exceptions)
    adjust_captions(bird_data)
    data_checks(bird_data)
    insert_abundance_data(bird_data)
    insert_rarity_descriptions(bird_data, uncommon_threshold, common_threshold)
    bird_data = dict(sorted(bird_data.items(), key=sortable_bird_key))
    return bird_data


@persist
def scrape_all_data(domain, prefix):
    # Find links to bird pages
    print(f"Searching for birds...")
    links = get_bird_links(domain, prefix)
    print(f"Found {len(links)} birds")

    # Get bird data
    bird_data = dict()
    for link in links:
        data = get_bird_data(domain + link)
        name = data["name"]
        bird_data[name] = data
        print(f"{len(bird_data)}/{len(links)}: {name}", end=" "*20 + "\r")

    return bird_data


@persist
def get_soup(url):
    # GET the url and check success
    response = requests.get(url)
    if response.status_code != 200:
        raise Exception(f"Failed to retrieve page at {url}.\nStatus code {response.status_code}")

    # Parse the HTML content of the page
    return BeautifulSoup(response.text, "html.parser")


class NoSuchElementException(Exception):
    pass


def get_bird_data(url):
    soup = get_soup(url)

    # Plan for fields to extract
    fields = {
        "name": ("species-hero", "species-hero__page-title", True),
        "population": ("species-measurements-population__population", "dl", False),
        "images": ("species-gallery", "img", False),
        "info": ("species-hero__stats", "species-hero__stats-item", False),
    }

    # Extract the fields
    for name in fields:
        parent_class, item_type, unique = fields[name]
        try:
            fields[name] = extract_html_items(soup, parent_class, item_type, unique)
        except NoSuchElementException as e:
            print(f"WARNING: missing {name} data in {url}")
            fields[name] = None

    # Use fields as needed
    data = {
        "name": inner_text(fields["name"]),
        "scientific-name": search_info(fields["info"], "Scientific name", "strong", url),
        "family": search_info(fields["info"], "family", "a", url),
        "population": extract_dls(fields["population"], True) if fields["population"] else None,
        "images": [{"caption": img["alt"], "url": img["data-src"]} for img in fields["images"]],
        "url": url,
    }

    return data


def extract_html_items(soup, parent_class, item_type, unique=False):
    """Find all items of item_type inside the unique item of parent_class"""
    parents = soup.find_all(class_=parent_class)
    if len(parents) == 0:
        raise NoSuchElementException(parent_class)
    assert len(parents) == 1
    parent = parents[0]
    if item_type in ["dl", "img", "div", "strong"]:  # type of element
        items = parent.find_all(item_type)
    else:  # class of element
        items = parent.find_all(class_=item_type)
    if unique:
        if len(items) != 1:
            raise Exception(f"Expected 1 item of type {item_type} but found {len(items)}")
        return items[0]
    return items


def extract_dls(dls, remove_colons=False):
    out = dict()

    for dl in dls:
        # Get terms and definitions
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        assert len(dts) == len(dds)

        # Process them into the dict
        for i in range(len(dts)):
            term = inner_text(dts[i])
            if remove_colons:
                assert term[-1] == ":"
                term = term[:-1]
            defn = inner_text(dds[i])
            out[term] = defn

    return out


def inner_text(item):
    rows = [row.strip() for row in item.contents]
    return " ".join(rows)


def search_info(items, keyword, tag_type, url):
    for item in items:
        keyword_text = item.find(string=lambda t: keyword in t)
        if keyword_text:
            keyword_link = keyword_text.next
            assert keyword_link.name == tag_type
            return inner_text(keyword_link)
    print(f"WARNING: missing {keyword} data in {url}")
    return None


def get_bird_links(domain, prefix):
    page = 1
    links = []
    while len(next_links := get_bird_links_page(domain, prefix, page)) != 0:
        links += next_links
        page += 1
    return links


def get_bird_links_page(domain, prefix, page):
    link = domain + prefix + str(page)
    soup = get_soup(link)
    items = extract_html_items(soup, "bird-browser", "BirdSpecies", False)
    return [item["href"] for item in items]


def apply_exceptions(bird_data, exceptions):
    """Fill in gaps in RSPB data with hard-coded values"""
    for species, field, value in exceptions:
        assert species in bird_data
        assert field in bird_data[species]
        print(f'NOTE: adding custom value "{value}" for {field} of {species}')
        if bird_data[species][field] is not None:
            print(f"WARNING: overwriting original value '{bird_data[species][field]}'")
        bird_data[species][field] = value


def adjust_captions(bird_data):
    for species in bird_data:
        for image in bird_data[species]["images"]:
            #if not image["caption"].startswith(species):
            #    print(species, "=", image["caption"])
            brackets = re.search("\((.*)\)$", image["caption"])
            if brackets:
                qualifier = brackets.group(1)
                if qualifier == "feral pigeon":
                    qualifier = ""
                qualifier = qualifier.replace(" / ", "/")
                qualifier = qualifier.replace("Dark", "dark")
            else:
                qualifier = ""
            image["caption"] = qualifier


def insert_abundance_data(bird_data):
    """Add heuristic abundance figures to bird_data

    Population data for a species comes in a variety of formats: numbers of
    pairs, birds, nests, ranges, information on specific areas and so on.  For
    many species, there are also separate figures for breeding, wintering and
    passage.  This is useful info, but in order to sort birds by how common they
    are, we need to process and combine this data into an overall score for each
    species, which we call abundance.

    This number will be roughly based on the maximum number of birds in the UK
    at any time in the year.  A big penalty is applied if the birds are only
    passage migrants.

    This function goes through all the species in bird_data, inspects their
    population attributes, and assigns them an abundance score, modifying the
    original dictionary.

    """
    for species in bird_data:
        population = bird_data[species]["population"]
        bird_data[species]["abundance"] = get_abundance_from_population(population)


def get_abundance_from_population(population):
    # Check we have *some* UK figures
    assert "UK breeding" in population or "UK wintering" in population or "UK passage" in population

    # Get a figure for each type of population
    abundance = dict()
    for pop_type in population:
        if pop_type != "Europe":  # ignore Europe
            assert pop_type in ["UK breeding", "UK wintering", "UK passage"]
            value = get_abundance_from_string(population[pop_type])
            if pop_type == "UK passage":  # passage migrants don't stay long
                value //= 5
            abundance[pop_type] = value
    assert len(abundance) > 0

    # Combine types
    value = max(abundance.values())

    # Special case to down-rank passage migrants

    return value


def get_abundance_from_string(s):
    """Process a string describing a population into an estimate for the total number of birds."""
    # Remove commas from numbers
    s = re.sub(",(\\d{3})", "\\g<1>", s)

    # Average out ranges
    m = re.search("([\\d.]+) *- *([\\d.]+)", s)
    if m:
        a = float(m.group(1))
        b = float(m.group(2))
        average = int((a + b) / 2)
        s = re.sub(m.group(0), str(average), s)

    # Handle "+" or "more than"
    m = re.search("([\\d.]+) *\\+", s) or re.search("More than ([\\d.]+)", s)
    if m:
        num = int(float(m.group(1)) * 1.1)
        s = s.replace(m.group(0), str(num))

    # Integrate the word "million"
    m = re.search("([\\d.]+) *million", s)
    if m:
        num = int(float(m.group(1)) * 10**6)
        s = re.sub(m.group(0), str(num), s)

    # Delete some redundant words
    if s.startswith("c"):  # for 'circa'
        s = s[1:]
    redundant_patterns = [
        " *\(plus .* in Ireland\)",
        "Around *",
        "Estimated *",
        " *in spring",
        " *\(spring\)",
        " *\(Jersey\)",
        " *in Great Britain;.*$",
        " *\(\\d{4} national survey\)",
        " *\(\\d{4} estimate\)\)",  # note weird extra bracket
    ]
    for pattern in redundant_patterns:
        s = re.sub(pattern, "", s)

    # Find a match
    forms = [
        ("(\\d+)", 1),
        ("(\\d+) birds?", 1),
        ("(\\d+) individuals?", 1),
        ("(\\d+) pairs?", 2),
        ("(\\d+) females?", 2),
        ("(\\d+) males?", 2),
        ("(\\d+) territories", 2),
        ("(\\d+) nests", 3),
        ("Approx. (\\d+) records a year", 1),
        ("Between (\\d+) \(in influx years\)", 0.5),
        ("(\\d+) birds \(incl. Ireland\)", 0.6),
        ("(\\d+) *\- *calling males Scotland", 2),
        ("(\\d+) birds from the .* population", 1),
        ("(\\d+) from .*, (\\d+) from .* and (\\d+) from .*", 1),
    ]
    for pattern, multiplier in forms:
        m = re.match("^" + pattern + "$", s)
        if m:
            total = sum([int(num) for num in m.groups()])
            out = int(total * multiplier)
            return out

    # Special values
    if s == "Hundreds":
        return 300
    elif s == "Very rare":
        return 2

    print(f"WARNING: could not parse a number of birds from '{s}', using 0 instead")
    return 0


def insert_rarity_descriptions(bird_data, uncommon_threshold, common_threshold):
    for species in bird_data:
        abundance = bird_data[species]["abundance"]
        if abundance >= common_threshold:
            rarity = "common"
        elif abundance >= uncommon_threshold:
            rarity = "uncommon"
        else:
            rarity = "rare"
        bird_data[species]["rarity"] = rarity


def sortable_bird_key(item):
    _, data = item
    rarity = ["common", "uncommon", "rare"].index(data["rarity"])
    family = data["family"]
    scientific_name = data["scientific-name"]
    return (rarity, family, scientific_name)


def data_checks(bird_data):
    pop_types = sorted(set([key for species in bird_data if bird_data[species]["population"] is not None for key in bird_data[species]["population"].keys()]))
    print(f"Population types ({len(pop_types)}):", ", ".join(pop_types))
    pop_formats = sorted(set([re.sub("\\d[\\d,.]*", "X", value) for rec in bird_data.values() for value in rec["population"].values()]))
    print(f"Population formats ({len(pop_formats)})")
    families = sorted(set([rec["family"] for rec in bird_data.values()]))
    print(f"Families ({len(families)})")


def dump_dictionary(data, filename):
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)
        print("Saved data to", filename)


def download_bird_images(bird_data, domain, dir_name):
    num_images = len([image for species in bird_data for image in bird_data[species]["images"]])

    if not os.path.isdir(dir_name):
        print("Making new directory", dir_name + "/")
        os.mkdir(dir_name)

    print(f"Downloading {num_images} images to {dir_name}...")
    done = 0
    for species in bird_data:
        for image in bird_data[species]["images"]:
            url = domain + image["url"]
            m = re.match("^(" + domain + ".*/([^/]*.jpg))?.*$", url)
            assert m
            url = m.group(1)
            filename = m.group(2)
            image["url"] = url
            image["filename"] = filename
            path = dir_name + "/" + filename
            done += 1
            print(f"{done}/{num_images}: {filename}", " " * 30, end="\r")
            if not os.path.isfile(path):
                urllib.request.urlretrieve(url, path)


def write_anki_csv(data, filename):
    header = """#separator:Semicolon
#html:true
#columns:Name;Scientific name;Family;Images;Population;URL;Abundance;Rarity
#notetype:Bird species
#deck:UK Birds
"""
    with open(filename, "w") as f:
        f.write(header)
        for name in data:
            species = data[name]
            fields = (
                species["name"],
                species["scientific-name"],
                species["family"],
                card_images(species["images"]),
                card_population(species["population"]),
                species["url"],
                str(species["abundance"]),
                species["rarity"],
            )
            for i in range(len(fields)):
                if ";" in fields[i]:
                    fields = list(fields)
                    fields[i] = '"' + fields[i] + '"'
                    fields = tuple(fields)

            f.write(";".join(fields) + "\n")
        print("Wrote out Anki data to", filename)


def card_images(images):
    return "<hr>".join([f"<figure><img src='{image['filename']}'><figcaption>{image['caption']}</figcaption></figure>" for image in images])


def card_population(population):
    return " ".join([f"<div class='poptype'>{pop_type}</div> <div class='popvalue'>{population[pop_type]}</div>" for pop_type in population])


# Clear caches
# get_soup.cache.clear()
# scrape_all_data.cache.clear()

# Parameters and custom data
domain = "https://www.rspb.org.uk"
prefix = "/birds-and-wildlife/wildlife-guides/bird-a-z/?Page="
exceptions = [
    ("Great shearwater", "population", {"UK passage": "Very rare"}),
    ("Grey phalarope", "population", {"UK passage": "200 birds"}),
    ("Little auk", "population", {"UK wintering": "Very rare"}),
    ("Long-tailed skua", "population", {"UK passage": "Very rare"}),
    ("Pomarine skua", "population", {"UK passage": "Very rare"}),
    ("Red-crested pochard", "family", "Ducks, geese and swans"),
    ("Snow goose", "family", "Ducks, geese and swans"),
    ("Sooty shearwater", "population", {"UK passage": "Very rare"}),
]
uncommon_threshold = 1000
common_threshold = 100000

# Get the full dictionary
bird_data = get_bird_dictionary(domain, prefix, exceptions, uncommon_threshold, common_threshold)

# Get the images
download_bird_images(bird_data, domain, "images")

# Write out a CSV file for Anki
write_anki_csv(bird_data, "bird-data.csv")

# Dump dictionary to json
dump_dictionary(bird_data, "bird-data.json")
