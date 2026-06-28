# Eezy Claim - Wound Billing Care Frontend

Static, deployable dashboard for `data/results.json`.

## Run Locally

```bash
python3 -m http.server 5173 --directory frontend
```

Open `http://localhost:5173`.

## Refresh Data

Run the pipeline, then copy the latest output into the frontend payload:

```bash
python3 pipeline.py
cp data/results.json frontend/results.json
```

## Deploy

Deploy the `frontend/` directory to any static host.
