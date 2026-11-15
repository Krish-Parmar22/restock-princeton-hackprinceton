# Running the camera on the Arduino UNO Q

The UNO Q's Linux side runs `main.py` unchanged. You draw the boxes from your laptop's browser, and the live feed shows there too.

```
webcam ─USB─ hub ─USB-C─ UNO Q (main.py) ──Wi-Fi/Tailscale──▶ laptop API :8000 ──▶ d1 + YOLO, ledger, agent, Photon
                                   └── :8090 live view + box editor ──▶ laptop browser
```

## 1. Hardware
1. Plug the webcam into the powered USB-C hub (the Anker 5-in-1).
2. Plug USB-C power into the hub's PD port.
3. Plug the hub into the UNO Q.
4. Point the webcam **down at the bins** from above. The bins should fill most of the picture, with no screens, windows or walkways inside a box.

## 2. Network and login
- **Wi-Fi:** set it up with Arduino App Lab's first-run setup, which also sets the password.
- **Log in:** `ssh arduino@<board-name>.local`.
- **If the venue Wi-Fi blocks device-to-device traffic** (common at hackathons), put both machines on Tailscale:
  ```bash
  curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
  ```
  Then use the laptop's Tailscale name or IP everywhere below.

## 3. Install (on the UNO Q)
```bash
sudo apt-get update && sudo apt-get install -y python3-opencv python3-numpy python3-requests git
git clone https://github.com/Krish-Parmar22/restock-princeton-hackprinceton.git   # or scp the hardware/ folder over
cd restock-princeton-hackprinceton/hardware
ls /dev/video*                                                     # the webcam is usually /dev/video0
```

## 4. Laptop side
The API has to listen on the network, not just localhost:
```bash
uv run uvicorn app.api.server:app --host 0.0.0.0 --port 8000
```
Set `UPLOAD_TOKEN` in the laptop `.env` and pass the same value to the board.

## 5. Run (on the UNO Q)
```bash
python3 main.py --camera 0 --source webcam --no-record --stream-port 8090 \
  --api-url http://<laptop>:8000 --upload-token <token> --camera-id unoq-makeline-1
```

## 6. Draw the boxes from the laptop
Open `http://<board-name>.local:8090/?key=<token>` (or the board's Tailscale IP).
1. Pick the ingredient.
2. Click the bin's corners on the picture.
3. Click **Finish box**, and repeat for each bin.
4. Click **Save boxes**.

The camera starts using the new boxes immediately. It also writes them to `zones.json`, so they survive a restart.

In the live view, a **green** box is idle and a **red** box has motion. A box that stays red with nobody touching the bin has something moving inside it: a screen, a doorway or a person. Redraw it tighter.

## What to expect
- A grab is sent about 8–10 s after the hand leaves. The camera waits for the bin to settle so that a paused hand doesn't split one grab in two.
- Nonstop motion is cut into 60 s grabs, so a busy line never blocks uploads.
- If the laptop is offline, grabs queue on the board and upload when it's back.
