import pandas as pd
import os
import re

INPUT_FILE = "data/raw_search_results.csv"
OUTPUT_FILE = "data/screened_papers.csv"

def screen_papers():
    if not os.path.exists(INPUT_FILE):
        print(f"File not found: {INPUT_FILE}. Please run step2 first.")
        return

    print("Reading raw data...")
    try:
        df = pd.read_csv(INPUT_FILE)
    except pd.errors.EmptyDataError:
        print("The raw file is empty.")
        return

    initial_count = len(df)
    print(f"Initial total papers: {initial_count}")

    df['title'] = df['title'].fillna('').astype(str)
    df['abstractText'] = df['abstractText'].fillna('').astype(str)
    
    
    df['full_text'] = (df['title'] + ' ' + df['abstractText']).str.lower()

    # ---------------- Rule 1: Deduplicate by ID ----------------
    df = df.drop_duplicates(subset=['id'])
    print(f"Remaining after deduplication: {len(df)} papers")

    # ---------------- Rule 2: Exclude reviews ----------------
    
    exclude_review_terms = r'(review|editorial|perspective|meta-analysis|commentary)'
    df = df[~df['full_text'].str.contains(exclude_review_terms, regex=True, na=False)]
    print(f"Remaining after excluding reviews: {len(df)} papers")

    # ---------------- Rule 3: Must contain microalgae ----------------
    
    include_algae_terms = r'(microalga|microalgae|chlorella|spirulina|scenedesmus|chlamydomonas|cyanobacteria|algae)'
    df_after_algae = df[df['full_text'].str.contains(include_algae_terms, regex=True, na=False)]
    print(f"Remaining after ensuring microalgae inclusion: {len(df_after_algae)} papers")

    
    print("\n--- Preview: Sample abstract (if any) after Rule 3 ---")
    if not df_after_algae.empty:
        print(df_after_algae.iloc[0]['abstractText'][:300] + "...")
    print("-" * 40)

    # ---------------- Rule 4: Must contain electrical ----------------
    
    include_electrical_terms = r'(voltage|current|power|electricity|electric|bpv|electrode|bioelectric|electron transfer|anode|cathode|volt|watt|ohm|amper|energy)'
    df_final = df_after_algae[df_after_algae['full_text'].str.contains(include_electrical_terms, regex=True, na=False)]
    print(f"Remaining after ensuring electrical data inclusion: {len(df_final)} papers")

    
    print("\n--- Preview: Titles of final remaining papers ---")
    for title in df_final['title'].head(5):
        print(f"- {title}")

    
    df_final = df_final.drop(columns=['full_text'])

    # ---------------- Save results ----------------
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    df_final.to_csv(OUTPUT_FILE, index=False, encoding='utf-8-sig')
    
    print("-" * 30)
    print(f"Screening complete! Reduced from {initial_count} to {len(df_final)} papers.")
    print(f"Results saved to: {OUTPUT_FILE}")
    print("-" * 30)

if __name__ == "__main__":
    screen_papers()