import os
import json
from flask import Flask, render_template, jsonify
import paho.mqtt.client as mqtt

app = Flask(__name__)

# --- EMQX Cloud Configuration ---
MQTT_BROKER = os.environ.get("MQTT_BROKER", "n6966951.ala.us-east-1.emqxsl.com")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 8883))
MQTT_USER = os.environ.get("MQTT_USER", "chicomiko")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "chicomiko123")
MQTT_TOPIC = "giftbox/trigger"

def get_mqtt_client(client_id):
    """Compatible with both paho-mqtt v1.x and v2.x"""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        return mqtt.Client(client_id=client_id)

def send_open_command():
    """Connects, transmits the payload to EMQX, and cleanly disconnects."""
    client_id = f"RenderKiosk-{os.urandom(4).hex()}"
    client = get_mqtt_client(client_id)
    
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.tls_set()
    client.tls_insecure_set(True)
    
    # Connect with a 10s timeout
    client.connect(MQTT_BROKER, MQTT_PORT, 10)
    
    # Start the worker thread
    client.loop_start()
    
    # Publish 'open' and block until the broker confirms delivery
    msg_info = client.publish(MQTT_TOPIC, "open", qos=1)
    msg_info.wait_for_publish(timeout=5.0)
    
    # Clean shutdown of the worker network thread
    client.loop_stop()
    client.disconnect()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/claim", methods=["POST"])
def claim_box():
    try:
        send_open_command()
        return jsonify({"status": "success", "message": "Gift box triggered!"}), 200
    except Exception as e:
        print(f"MQTT Publish Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
