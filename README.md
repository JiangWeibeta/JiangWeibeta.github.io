# Academic homepage

A one-page academic homepage for **Wei Jiang** — About Me, Career, Education,
Publications, Awards and Service — built for GitHub Pages with plain Jekyll.

- **Single page, two columns.** The identity card on the left is sticky; the
  sections scroll on the right.
- **No build step, no CI, no Gemfile.** GitHub Pages runs Jekyll for you: push
  the branch and the site is online a minute later.
- **No Sass, no framework, no web fonts, no JavaScript dependency.** One
  stylesheet, one small inline script for the navigation highlight.
- **All of the text lives in two data files**, so editing the page never means
  touching markup.

```
_config.yml               site-wide settings (title, description, baseurl)
_data/profile.yml         name, bio, links, career, education, awards, service
_data/publications.yml    the publication list, grouped by research area
_layouts/default.html     the HTML shell (head, meta tags, stylesheet)
index.html                the page itself — the two-column layout
404.html                  a matching "page not found" page
assets/css/style.css      the whole design, including dark mode and print
assets/images/portrait.jpg
preview.py                local preview, Python 3 standard library only
```

## Editing the site

Everything a reader sees comes from `_data/`.

`_data/publications.yml` is grouped by research area — add a paper to the
matching group (newest first), or add a new group; a group without papers is
simply not rendered:

```yaml
categories:
  - name: "Neural Video Compression"
    papers:
      - year: "2026"
        title: "Paper title"
        authors: "<strong>Wei Jiang</strong>, Co Author"
        venue: "Conference or journal name"
        tags: ["CCF-A", "Oral"]      # optional badges
        highlight: "One sentence about the contribution."
        links:                       # optional
          - label: "PDF"
            url: "https://arxiv.org/pdf/0000.00000"
```

Available badge styles are `CCF-A` (teal), `Oral` (rust) and `Preprint` (gold);
any other tag falls back to a neutral grey.

`_data/profile.yml` holds `intro`, `links`, `career`, `education`, `awards` and
`service`. Every entry in `career`, `education` and `awards` uses the same shape
— `title`, `subtitle`, `period` and an optional `note` — and `service` items
carry the venue written out in full with its abbreviation plus the years:

```yaml
service:
  - label: "Conferences"
    items:
      - name: "International Conference on Machine Learning (ICML)"
        years: "2025"
```

## Preview locally

No installation, no gems:

```bash
python3 preview.py            # http://127.0.0.1:4000
python3 preview.py --port 8080
python3 preview.py --build    # write the rendered site into _site/
python3 preview.py --check    # validate the YAML data files
```

`preview.py` reads `_config.yml`, `_data/*.yml`, the layouts and front matter,
and renders the same Liquid this site uses (`{{ output }}`, `assign`, `for`,
`if/elsif/else`, and the `relative_url`, `default`, `slugify`, `join`, `size`,
`strip`, `downcase`, `upcase`, `append`, `prepend`, `replace`, `escape` filters).
Anything outside that subset stops with an explicit error rather than rendering
something different from GitHub Pages.

If you do have Ruby and Jekyll installed, the usual route works as well:

```bash
jekyll serve --baseurl ""
```

## Publishing on GitHub Pages

This repository is already a git repository with one commit on `main`, and it is
set up as a **user site**: the site lives at `https://jiangweibeta.github.io/`.

1. On GitHub, create a **new empty repository** named `JiangWeibeta.github.io`
   (no README, no .gitignore — an empty repository keeps the first push simple).
2. Push this repository to it:

   ```bash
   cd ~/Documents/homepage
   git remote add origin git@github.com:JiangWeibeta/JiangWeibeta.github.io.git
   git push -u origin main
   ```

   Without an SSH key on your GitHub account, use HTTPS instead and paste a
   personal access token when git asks for a password:

   ```bash
   git remote add origin https://github.com/JiangWeibeta/JiangWeibeta.github.io.git
   git push -u origin main
   ```

3. In the repository, open **Settings → Pages**, choose **Deploy from a branch**,
   select `main` and the `/ (root)` folder, then save.

GitHub Pages builds the site with Jekyll automatically on every push — there is
no workflow to maintain and nothing to install. The first build takes a minute or
two; afterwards <https://jiangweibeta.github.io/> serves the page.

Every later update is just:

```bash
cd ~/Documents/homepage
python3 preview.py                 # look at the change locally first
git add -A && git commit -m "Add CLIC 2025 paper"
git push
```

**Project sites.** For a repository with any other name the site is served from
`https://<username>.github.io/<repository>`, so the path prefix has to be set in
`_config.yml`:

```yaml
url: "https://<username>.github.io"
baseurl: "/<repository>"
```

## Notes

- `assets/css/style.css` is a plain stylesheet without front matter, so Jekyll
  copies it verbatim — nothing is compiled.
- Dark mode follows the reader's system setting; the page also has print styles,
  so printing or "Save as PDF" gives a clean, single-column CV.
- Icons in the sidebar are inline SVG symbols defined once at the top of
  `index.html`; `_data/profile.yml` just names one (`mail`, `scholar`, `orcid`,
  `github`, `twitter`, `linkedin`).
- `.gitignore` keeps `SimpleResume/` (67 MB of CJK fonts) and the `IMG_*.JPG`
  reference screenshots (30 MB) out of the repository. Delete either line there
  if you want that material committed; the repository as it stands is about
  200 KB.
