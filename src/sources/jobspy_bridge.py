import sys
import json
import argparse
from jobspy import scrape_jobs

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--location", default="Remote")
    parser.add_argument("--sites", default="glassdoor,zip_recruiter,google")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    site_names = [s.strip() for s in args.sites.split(",") if s.strip()]

    # Run jobspy
    try:
        jobs_df = scrape_jobs(
            site_name=site_names,
            search_term=args.keywords,
            location=args.location,
            results_wanted=args.limit,
            hours_old=72, # last 3 days
        )
        
        results = []
        if not jobs_df.empty:
            # fill nan with empty string
            jobs_df = jobs_df.fillna("")
            for _, row in jobs_df.iterrows():
                results.append({
                    "title": row.get("title", ""),
                    "company": row.get("company", ""),
                    "location": row.get("location", ""),
                    "url": row.get("job_url", ""),
                    "salary": row.get("salary_source", "") or str(row.get("min_amount", "")),
                    "description": row.get("description", ""),
                    "site": row.get("site", ""),
                })
        print(json.dumps(results))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
