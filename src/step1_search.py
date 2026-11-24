
import urllib.request
import urllib.parse
import json
import csv
import os
import time

API_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

QUERY = (
    'ABSTRACT:(microalgae OR microalga OR chlorella OR spirulina OR scenedesmus '
    'OR chlamydomonas OR cyanobacteria OR synechocystis OR synechococcus '
    'OR nostoc OR diatom OR microalgal OR algal OR cyanobacterial '
    'OR cyanobacterium OR phototroph OR photoautotroph '
    'OR "photosynthetic microorganism") AND '
    'ABSTRACT:(biophotovoltaic OR bio-photovoltaic OR photocurrent '
    'OR photo-current OR bioelectrochemical OR photo-bioelectrochemical '
    'OR bio-photoelectrochemical OR "photosynthetic microbial fuel cell" '
    'OR photoMFC OR PMFC OR BPV OR electricity OR voltage '
    'OR "power density" OR current)'
)

PAGE_SIZE = 50
OUTPUT_FILE = "data/raw_search_results.csv"


def get_europe_pmc_data(params, retries=3):
    """
    Request one page of results from Europe PMC.
    Retry automatically if the request fails.
    """

    query_string = urllib.parse.urlencode(params)
    full_url = API_URL + "?" + query_string

    request = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": "MicroalgaeMatch/1.0"
        }
    )

    # Use the system's normal network connection.
    # If you need to use a proxy, remove ProxyHandler({}).
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )

    for attempt in range(retries):
        try:
            with opener.open(request, timeout=60) as response:
                content = response.read().decode("utf-8")
                return json.loads(content)

        except Exception as e:
            print(
                f"Request failed "
                f"(attempt {attempt + 1}/{retries}): {e}"
            )

            if attempt < retries - 1:
                time.sleep(3)
            else:
                raise


def search_europe_pmc():

    try:
        # Create output directory
        output_dir = os.path.dirname(OUTPUT_FILE)

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        print("Output file:", os.path.abspath(OUTPUT_FILE))

        # --------------------------------------------------
        # 1. Load existing IDs for duplicate removal
        # --------------------------------------------------

        existing_ids = set()

        if os.path.exists(OUTPUT_FILE):

            with open(
                OUTPUT_FILE,
                "r",
                encoding="utf-8-sig",
                newline=""
            ) as f:

                reader = csv.DictReader(f)

                for row in reader:
                    paper_id = row.get("id", "")

                    if paper_id:
                        existing_ids.add(paper_id)

            print(
                f"Found existing file with "
                f"{len(existing_ids)} papers. Resuming..."
            )

            file_mode = "a"

        else:
            file_mode = "w"

        # --------------------------------------------------
        # 2. Set up Europe PMC search parameters
        # --------------------------------------------------

        params = {
            "query": QUERY,
            "format": "json",
            "resultType": "core",
            "pageSize": PAGE_SIZE,
            "cursorMark": "*"
        }

        # Get total number of matching records
        first_response = get_europe_pmc_data(params)

        hit_count = first_response.get("hitCount", 0)

        print("Total search results:", hit_count)

        fieldnames = [
            "id",
            "source",
            "title",
            "abstractText",
            "pubYear",
            "doi"
        ]

        # --------------------------------------------------
        # 3. Write results and paginate using cursorMark
        # --------------------------------------------------

        with open(
            OUTPUT_FILE,
            file_mode,
            newline="",
            encoding="utf-8-sig"
        ) as csvfile:

            writer = csv.DictWriter(
                csvfile,
                fieldnames=fieldnames
            )

            if file_mode == "w":
                writer.writeheader()

            # The first response has already been requested.
            data = first_response

            cursor_mark = "*"
            page = 1

            while True:

                results = data.get(
                    "resultList", {}
                ).get("result", [])

                if not results:
                    print("No more results.")
                    break

                new_papers_count = 0

                page_ids = []

                for paper in results:

                    paper_id = paper.get("id", "")

                    if not paper_id:
                        continue

                    page_ids.append(paper_id)

                    # Skip papers already saved
                    if paper_id in existing_ids:
                        continue

                    row = {
                        "id": paper_id,
                        "source": paper.get("source", ""),
                        "title": paper.get("title", ""),
                        "abstractText": paper.get(
                            "abstractText", ""
                        ),
                        "pubYear": paper.get("pubYear", ""),
                        "doi": paper.get("doi", "")
                    }

                    writer.writerow(row)

                    existing_ids.add(paper_id)
                    new_papers_count += 1

                # Flush data to disk after every page
                csvfile.flush()

                first_id = page_ids[0] if page_ids else ""
                last_id = page_ids[-1] if page_ids else ""

                print(
                    f"Retrieved page {page} "
                    f"(Returned {len(results)} records, "
                    f"Added {new_papers_count} new papers)"
                )

                print(
                    f"  First ID: {first_id} | "
                    f"Last ID: {last_id}"
                )

                # --------------------------------------------------
                # 4. Get next cursor
                # --------------------------------------------------

                next_cursor_mark = data.get(
                    "nextCursorMark"
                )

                # Stop if no next cursor or cursor unchanged
                if (
                    not next_cursor_mark
                    or next_cursor_mark == cursor_mark
                ):
                    print("Pagination finished.")
                    break

                cursor_mark = next_cursor_mark

                # Prepare request for the next page
                params["cursorMark"] = cursor_mark

                page += 1

                time.sleep(0.5)

                # Request next page
                data = get_europe_pmc_data(params)

        print()
        print("Search completed.")
        print("Total unique papers saved:", len(existing_ids))
        print("Results saved to:", os.path.abspath(OUTPUT_FILE))

    except KeyboardInterrupt:
        print("\nSearch interrupted by user.")

    except Exception as e:
        print(f"Stopped due to error: {e}")


if __name__ == "__main__":
    search_europe_pmc()
