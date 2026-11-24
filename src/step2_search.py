import pandas as pd
import os
import re

INPUT_FILE = "data/raw_search_results.csv"
OUTPUT_FILE = "data/screened_papers.csv"

def screen_papers():
    if not os.path.exists(INPUT_FILE):
        print(f"File not found: {INPUT_FILE}.")
        return

    print("Reading raw data...")
    try:
        df = pd.read_csv(INPUT_FILE)
    except pd.errors.EmptyDataError:
        print("The raw file is empty.")
        return

    initial_count = len(df)
    print(f"Initial total papers: {initial_count}")

    # Data cleaning
    df['title'] = df['title'].fillna('').astype(str)
    df['abstractText'] = df['abstractText'].fillna('').astype(str)
    df['full_text'] = (df['title'] + ' ' + df['abstractText']).str.lower()

    # Deduplicate based on ID
    df = df.drop_duplicates(subset=['id'])
    print(f"After deduplication: {len(df)}")

    # Exclude reviews
    exclude_review_terms = r'(?:review|editorial|perspective|meta-analysis|commentary)'
    df = df[~df['full_text'].str.contains(exclude_review_terms, regex=True, na=False)]
    print(f"After excluding reviews: {len(df)}")


    # Must contain conditions (Hard requirement)
    condition_terms = r'(?:stress|stresses|limitation|limited|starvation|starved|deprivation|deprived|deficien|deficiency|deficient|salinity|salt|saline|heat|temperature|cold|light intensity|light stress|darkness|nutrient|nitrogen|phosphorus|phosphate|carbon|co2|ph\s|acidity|alkaline|oxidative|reactive oxygen|ros\b|morphology|morphological|growth|biomass|productivity|cultivation|incubation|medium|treatment|condition|applied|chamber|reactor)'
    df = df[df['full_text'].str.contains(condition_terms, regex=True, na=False)]
    print(f"After ensuring condition inclusion: {len(df)}")

    # Flagging strain codes
    print("Flagging strain codes and conditions...")
    
    strain_pattern = r'\b(?:[A-Za-z]{2,6}\s*\d{1,5}|sp\.\s*[A-Za-z0-9-]+|strain\s*[A-Za-z0-9-]+)\b'
    
    df['strain_code_found'] = df['full_text'].apply(
        lambda x: ', '.join(set(re.findall(strain_pattern, x, re.IGNORECASE)))
    )
    
    df['has_strain_code'] = df['strain_code_found'].apply(lambda x: 'Yes' if x.strip() else 'No (Needs manual check)')

    conditions_to_extract = ['nitrogen', 'stress', 'salinity', 'temperature', 'light', 'medium', 'carbon', 'phosphate']
    df['conditions_mentioned'] = df['full_text'].apply(
        lambda x: ', '.join([c for c in conditions_to_extract if c in x])
    )

    # Accessbility status
    if 'isOpenAccess' in df.columns:
        df['access_status'] = df['isOpenAccess'].apply(lambda x: 'Accessible' if str(x).upper() == 'Y' else 'Inaccessible')
    else:
        df['access_status'] = 'Unknown (Check during download)'

    columns_to_keep = ['id', 'source', 'title', 'pubYear', 'doi', 'has_strain_code', 'strain_code_found', 'conditions_mentioned', 'access_status']
    df_final = df[columns_to_keep].copy()

    # Save results
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    df_final.to_csv(OUTPUT_FILE, index=False, encoding='utf-8-sig')
    
    print("-" * 30)
    print(f"Screening complete! Saved to: {OUTPUT_FILE}")
    print(f"Total highly relevant papers kept: {len(df_final)}")
    explicit_strain_count = len(df_final[df_final['has_strain_code'] == 'Yes'])
    print(f"Out of these, {explicit_strain_count} papers contain an explicit strain code.")

if __name__ == "__main__":
    screen_papers()