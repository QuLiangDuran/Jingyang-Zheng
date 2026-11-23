
import urllib.request
import urllib.parse
import json
import csv
import os
import math
import time

# Europe PMC API
API_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Search query
QUERY = (
    '(microalgae OR microalga) AND '
    '(electricity OR voltage OR "power density" OR current OR BPV)'
)

# Each request retrieves 50 papers
PAGE_SIZE = 50

# Output file
OUTPUT_FILE = "data/raw_search_results.csv"


def get_europe_pmc_data(params):

    query_string = urllib.parse.urlencode(params)
    full_url = API_URL + "?" + query_string

    request = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": "MicroalgaeMatch/1.0"
        }
    )

    
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )

    with opener.open(request, timeout=30) as response:
        response_data = response.read().decode("utf-8")

    return json.loads(response_data)

def search_europe_pmc():

    try:
        # Create output directory
        os.makedirs(
            os.path.dirname(OUTPUT_FILE),
            exist_ok=True
        )

        # First request: get total number of results
        params = {
            "query": QUERY,
            "format": "json",
            "pageSize": PAGE_SIZE,
            "page": 1
        }

        data = get_europe_pmc_data(params)

        hit_count = data.get("hitCount", 0)

        total_pages = math.ceil(
            hit_count / PAGE_SIZE
        )

        print(f"Total search results: {hit_count}")
        print(f"Total pages: {total_pages}")

        # CSV fields
        fieldnames = [
            "id",
            "source",
            "title",
            "abstractText",
            "pubYear",
            "doi"
        ]

        # Save results to CSV
        with open(
            OUTPUT_FILE,
            "w",
            newline="",
            encoding="utf-8-sig"
        ) as csvfile:

            writer = csv.DictWriter(
                csvfile,
                fieldnames=fieldnames
            )

            writer.writeheader()

            # Retrieve all pages
            for page in range(1, total_pages + 1):

                params["page"] = page

                data = get_europe_pmc_data(params)

                results = data.get(
                    "resultList", {}
                ).get("result", [])

                for paper in results:

                    row = {
                        "id": paper.get("id", ""),
                        "source": paper.get("source", ""),
                        "title": paper.get("title", ""),
                        "abstractText": paper.get(
                            "abstractText", ""
                        ),
                        "pubYear": paper.get("pubYear", ""),
                        "doi": paper.get("doi", "")
                    }

                    writer.writerow(row)

                print(
                    f"Retrieved page {page}/{total_pages}"
                )

                # Avoid sending requests too quickly
                time.sleep(0.2)

        print(
            f"Search completed. Results saved to: "
            f"{OUTPUT_FILE}"
        )

    except urllib.error.HTTPError as e:
        print(f"HTTP error: {e.code} - {e.reason}")

    except urllib.error.URLError as e:
        print(f"Network error: {e.reason}")

    except json.JSONDecodeError as e:
        print(f"Error parsing JSON response: {e}")

    except OSError as e:
        print(f"Error writing CSV file: {e}")


if __name__ == "__main__":
    search_europe_pmc()