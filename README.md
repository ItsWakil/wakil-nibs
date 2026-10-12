# Wakil Nibs

A small self-hosted web app for keeping track of your fountain pens and inks.

I built it because a spreadsheet kept losing the plot. I wanted to see what's
inked right now, what each bottle looks like, how much everything cost, and
have a place for photos and handwriting samples. It runs on a Flask app and a
single SQLite file, so there's no database server to babysit.

Anyone can look at your collection. Only you (with a password) can change it.

## What it does

- Pens and inks, each with a detail page, photos and notes
- A dashboard of what's inked right now, with totals for pens, ink, the whole
  collection, and how many mL of ink you have on hand
- A value field on everything, and status flags (pens can be loaned out, being
  serviced, broken or sold; inks can be sold or empty)
- Ink bottle icons that take on the color of the ink
- Multiple photos per pen, plus a handwriting sample for each pen and ink
- List and grid views, sortable by almost anything
- Light and dark themes

## Try it on your own machine

You need Python 3.8 or newer.

```bash
git clone https://github.com/ItsWakil/wakil-nibs.git
cd wakil-nibs
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

export PENTRACKER_PASSWORD="pick-something"
export PENTRACKER_SECRET_KEY="any-long-random-string"
python app.py
```

Open http://127.0.0.1:5000. The first run creates `pentracker.db` and adds one
sample pen and ink so the pages aren't empty. Delete them once you've added
your own. Click **Log in** in the top bar to unlock the add and edit buttons.

If you skip the two `export` lines the app still starts, but it will complain
and fall back to a password of `changeme`. Fine for a quick look, not fine for
anything other people can reach.

That `python app.py` server is for trying things out. For real use, run it
with gunicorn as described below.

## Running it for real

This is aimed at a Linux box that stays on, like a home server or a small VPS.
There are two ways to set it up.

### The quick way

```bash
bash setup.sh
```

It makes the virtual environment, asks you for a password, generates a secret
key, and offers to install a systemd service. It can also write an nginx config
for you to review. It never touches your nginx setup on its own.

### The manual way

1. Do the steps from "Try it on your own machine" above, but skip `python app.py`.
2. Copy `.env.example` to `.env` and fill in the password and secret key.
3. Copy `deploy/wakil-nibs.service.example` to `/etc/systemd/system/wakil-nibs.service`
   and fix the paths and username.
4. Start it:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now wakil-nibs
   sudo systemctl status wakil-nibs
   ```
5. It's now listening on `127.0.0.1:8000`. To put it on a domain, use
   `deploy/nginx.conf.example` as a starting point and add HTTPS with
   [certbot](https://certbot.eff.org/). Without HTTPS, your password crosses
   the network in plain text when you log in.

nginx can't run Python by itself. It sits in front and hands requests to
gunicorn, which is the thing actually running the app.

## Your data

Everything you enter lives in two places:

- `pentracker.db`, one SQLite file
- `static/uploads/`, where photos and handwriting samples go

Back those up together by copying them somewhere. To restore, put them back
in a fresh install. Both are in `.gitignore`, so they won't end up in git.

## Updating

Pull the new code (or copy the new files over), leave `pentracker.db` and
`static/uploads/` alone, then restart:

```bash
sudo systemctl restart wakil-nibs
```

Gunicorn caches templates, so a restart is needed after any change. If the
styling looks broken afterward, make sure `static/css/style.css` actually
got updated, since nginx serves that file straight from disk. A hard refresh
(Ctrl+Shift+R) rules out the browser cache.

## A few notes

- The setting names still start with `PENTRACKER_` and the database is still
  called `pentracker.db`. That's from before the app had a name. I left them
  alone so existing installs keep working.
- Adding an ink or pen needs the password. Viewing never does. If you want to
  hide the collection too, put nginx basic auth in front of it.
- It's a single-user app. There are no accounts, just the one editing password.

## License

MIT. See [LICENSE](LICENSE). Do what you like with it, including forking it
for your own collection.

Made by [Wakil](https://wakil.ink) · [GitHub](https://github.com/ItsWakil)
