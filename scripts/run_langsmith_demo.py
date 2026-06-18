import httpx

BASE_URL = "http://127.0.0.1:8000"

QUERIES = [
    {
        "message": "Show devices with low battery across the fleet",
        "company_id": "acme-001",
    },
    {
        "message": "Which devices fail os_up_to_date compliance checks?",
        "company_id": "globex-002",
    },
    {
        "message": "Propose remediation and replacement actions for fleet issues",
        "company_id": "acme-001",
    },
]


def main() -> None:
    with httpx.Client(timeout=180.0) as client:
        print("Health:", client.get(f"{BASE_URL}/health").json())
        for index, payload in enumerate(QUERIES, start=1):
            print(f"\n--- Query {index}: {payload['message']}")
            response = client.post(f"{BASE_URL}/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            print("Status:", data["status"])
            print("Thread:", data["thread_id"])
            preview = (data.get("final_response") or "")[:300]
            print("Response preview:", preview)
            if data["status"] == "paused":
                approval = client.post(
                    f"{BASE_URL}/approve",
                    json={"thread_id": data["thread_id"], "approved": True},
                )
                approval.raise_for_status()
                approved = approval.json()
                print("After approval:", approved["status"])
                print("Decision:", approved.get("approval_decision"))


if __name__ == "__main__":
    main()
