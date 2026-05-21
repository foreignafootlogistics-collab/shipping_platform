import requests


def get_driving_distance_km(origin, destination, api_key):
    try:
        url = "https://maps.googleapis.com/maps/api/distancematrix/json"

        params = {
            "origins": origin,
            "destinations": destination,
            "mode": "driving",
            "units": "metric",
            "key": api_key,
        }

        response = requests.get(url, params=params, timeout=15)
        data = response.json()

        if data.get("status") != "OK":
            return {"success": False, "error": data.get("status")}

        element = data["rows"][0]["elements"][0]

        if element.get("status") != "OK":
            return {"success": False, "error": element.get("status")}

        distance_km = element["distance"]["value"] / 1000

        return {
            "success": True,
            "distance_km": round(distance_km, 2),
            "duration_text": element["duration"]["text"],
        }

    except Exception as e:
        return {"success": False, "error": str(e)}