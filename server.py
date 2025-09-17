from flask import Flask, request, redirect, url_for, session, render_template, send_from_directory
import json
import os

BASE_DIR = os.path.dirname(__file__)
FILES_DIR = os.path.join(BASE_DIR, "files")

# Tell Flask to look for templates inside /files
app = Flask(__name__, template_folder="files", static_folder="files")
app.secret_key = "Secret@123"


# ------------------------
# Helpers
# ------------------------
def load_files():
    """Load all available [label, filename] pairs."""
    with open(os.path.join(BASE_DIR, "files.json")) as f:
        return json.load(f)

def load_permissions():
    """Load user credentials and access rules."""
    with open(os.path.join(BASE_DIR, "permissions.json")) as f:
        return json.load(f)


# ------------------------
# Routes
# ------------------------
@app.route("/", methods=["GET", "POST"])
def login():
    """Login page."""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        permissions = load_permissions()
        user = permissions.get(username)

        if user and user["password"] == password:
            session["user"] = username
            return redirect(url_for("index"))

        return """
        <div style="font-family:sans-serif; color:red; margin:20px;">
            Invalid credentials. <a href='/'>Try again</a>.
        </div>
        """, 403

    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Login</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light d-flex justify-content-center align-items-center vh-100">
        <div class="card shadow p-4" style="width: 350px;">
            <h3 class="text-center mb-3">🔐 Login</h3>
            <form method="post">
                <div class="mb-3">
                    <label class="form-label">Username</label>
                    <input name="username" class="form-control" placeholder="Enter username" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">Password</label>
                    <input name="password" type="password" class="form-control" placeholder="Enter password" required>
                </div>
                <button type="submit" class="btn btn-primary w-100">Login</button>
            </form>
        </div>
    </body>
    </html>
    """


@app.route("/index")
def index():
    """Render pre-generated index.html with only allowed files."""
    if "user" not in session:
        return redirect(url_for("login"))

    user = session["user"]
    all_files = load_files()        # [["file1", "file1.html"], ["file2", "file2.html"]]
    permissions = load_permissions()

    # Filter files based on access list in permissions.json
    allowed_labels = permissions[user]["access"]
    allowed_files = [f for f in all_files if f[0] in allowed_labels]

    return render_template("index.html", files=allowed_files, user=user)


@app.route("/file/<filename>")
def serve_file(filename):
    """Serve individual file if user has access."""
    if "user" not in session:
        return redirect(url_for("login"))

    user = session["user"]
    all_files = load_files()
    permissions = load_permissions()

    # Build lookup: label -> filename
    file_lookup = {label: fname for label, fname in all_files}
    allowed_labels = permissions[user]["access"]
    allowed_filenames = [file_lookup[label] for label in allowed_labels if label in file_lookup]

    if filename in allowed_filenames:
        return send_from_directory(FILES_DIR, filename)

    return "Access denied", 403


@app.route("/logout")
def logout():
    """Clear session and return to login."""
    session.clear()
    return redirect(url_for("login"))


# ------------------------
# Run App
# ------------------------
if __name__ == "__main__":
    app.run(debug=True)
