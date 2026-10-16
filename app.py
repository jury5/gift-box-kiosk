import json
import os
from flask import Flask, jsonify, render_template, request
import paho.mqtt.client as mqtt

app = Flask(__name__)

# --- EMQX Cloud Configuration ---
MQTT_BROKER = os.environ.get(
    "MQTT_BROKER", "n6966951.ala.us-east-1.emqxsl.com"
)
MQTT_PORT = int(os.environ.get("MQTT_PORT", 8883))
MQTT_USER = os.environ.get("MQTT_USER", "chicomiko")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "chicomiko123")
MQTT_TOPIC = "giftbox/trigger"


# Initialize MQTT Client
mqtt_client = mqtt.Client(client_id="RenderKioskBridge")
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
mqtt_client.tls_set()


def connect_mqtt():
  try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()
    print("Connected to EMQX Cloud successfully!")
  except Exception as e:
    print(f"Failed to connect to MQTT broker: {e}")


# Connect on startup
connect_mqtt()


@app.route("/")
def index():
  return render_template("index.html")

@app.route("/claim", methods=["POST"])
def claim_box():
  try:
    # Publish MQTT payload to trigger the ESP32 stepper motor
    payload = json.dumps({"action": "open", "source": "web_kiosk"})
    result = mqtt_client.publish(MQTT_TOPIC, payload)

    if result.rc == mqtt.MQTT_ERR_SUCCESS:
      return jsonify({"status": "success", "message": "Gift box triggered!"}), 200
    else:
      return jsonify({"status": "error", "message": "Failed to publish MQTT"}), 500
  except Exception as e:
    return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 5000))
  app.run(host="0.0.0.0", port=port)
