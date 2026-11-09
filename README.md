# Travel & Hospitality Strategy Intelligence AI

Compare hospitality, aviation, and travel companies using public SEC filings and issuer disclosures.
The Streamlit workspace includes financial trends, operating benchmarks, strategic signals,
hypothetical scenarios, and executive PDF briefings. Evidence includes source links and actual
reporting periods. Coverage varies by issuer and period.

## Run locally

Use Python 3.12 from the repository root.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export SEC_USER_AGENT="Your organization your-contact@example.org"
python -m streamlit run competitive_app.py
```

Use a real organization and contact email in your shell. The app retrieves and caches public
responses at runtime. It distinguishes missing sources, failed refreshes, and missing
observations. SEC or issuer access restrictions can leave a view without eligible observations.

For optional local AI, start Ollama on the same machine as Streamlit, select an installed model
such as `qwen3:4b`, and use **Test local model** before submitting a question. Calculated briefings
and PDF exports work independently of Ollama.

## Streamlit Community Cloud

Select the approved GitHub repository, the `main` branch, and `competitive_app.py` as the
entry point. Select Python 3.12 under Advanced settings. Set the following top-level key in
the deployment's Secrets settings with the operator's real organization and contact email.

```toml
SEC_USER_AGENT = "Your organization your-contact@example.org"
```

Community Cloud runs Streamlit on a hosted machine. The local Ollama integration expects a
model on that same machine, so use the calculated briefing on Community Cloud. Leave the model
field blank. The SQLite cache is created at runtime and can be lost on restart. If a hosted
request is denied by a data provider, the app reports the retrieval limitation.

- [Community Cloud dependency setup](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)
- [Community Cloud secrets settings](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)

## Data and tests

The app links to SEC filings and issuer disclosures. Financial charts combine only eligible series.
Operating definitions and calendar windows may differ. Scenarios model user assumptions and
report their baseline separately. No downloaded source data or local SQLite cache ships with the app.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```
