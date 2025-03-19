from flask import Flask, render_template, request, session, redirect, url_for
from pymongo import MongoClient
import os
import re
import pandas as pd
from bokeh.plotting import figure
from bokeh.models import ColumnDataSource, HoverTool, PanTool, WheelZoomTool, ResetTool
from bokeh.embed import components
from bokeh.palettes import Category10, viridis
from bokeh.resources import CDN

app = Flask(__name__)
app.secret_key = "your_secret_key"

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Connect to MongoDB
client = MongoClient("mongodb://localhost:27017/")
db = client["file_data"]
collection = db["uploaded_files"]

# Function to convert hex to decimal
def hex_to_decimal(hex_str):
    is_negative = False
    if hex_str.startswith('-'):
        is_negative = True
        hex_str = hex_str[1:]

    if '.' in hex_str:
        integer_part, fractional_part = hex_str.split('.')
    else:
        integer_part, fractional_part = hex_str, ''

    decimal_int = int(integer_part, 16) if integer_part else 0
    decimal_fraction = sum(int(digit, 16) / (16 ** (i + 1)) for i, digit in enumerate(fractional_part))
    decimal_value = decimal_int + decimal_fraction
    return -decimal_value if is_negative else decimal_value

# Process HEX file and save to MongoDB with CSV name
def process_hex_file(file_path):
    csv_filename = os.path.splitext(os.path.basename(file_path))[0] + ".csv"
    output_file_path = os.path.join(app.config["UPLOAD_FOLDER"], csv_filename)

    data = []
    with open(file_path, 'r') as hex_file:
        for line in hex_file:
            hex_values = re.findall(r'[0-9A-Fa-f.-]+', line.strip())
            decimal_values = [hex_to_decimal(hv) for hv in hex_values]
            data.append(decimal_values)

    db["uploaded_files"].insert_one({
        "file_name": csv_filename,
        "data": data
    })

    df = pd.DataFrame(data)
    df.to_csv(output_file_path, index=False, header=False)
    return output_file_path

# Retrieve data from MongoDB
def get_column_data(file_name, column_index):
    file_entry = db["uploaded_files"].find_one({"file_name": file_name})
    print(f"DB Query for {file_name}: {file_entry}")
    if not file_entry:
        print(f"No entry found in DB for {file_name}")
        return None

    data = file_entry.get("data", [])
    if not data:
        print(f"No data field in entry for {file_name}")
        return None

    try:
        column_data = [row[column_index] for row in data if len(row) > column_index]
        print(f"Column {column_index} data (first 10): {column_data[:10]}")
        return column_data
    except IndexError:
        print(f"Column index {column_index} out of range for {file_name}")
        return None

def get_column_count(files_data):
    if files_data:
        filepath = files_data[0]["path"]
        try:
            df = pd.read_csv(filepath)
            return len(df.columns)
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
    return None

@app.route("/", methods=["GET", "POST"])
def index():
    if "files_data" not in session:
        session["files_data"] = []

    selected_column_index = session.get("selected_column_index", None)

    if request.method == "POST":
        if "files[]" in request.files:
            files = request.files.getlist("files[]")
            for file in files:
                if file and file.filename:
                    file_ext = os.path.splitext(file.filename)[1].lower()
                    filepath = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
                    file.save(filepath)

                    if file_ext == ".hex":
                        filepath = process_hex_file(filepath)

                    session["files_data"].append({"name": os.path.basename(filepath), "path": filepath})
            session.modified = True
            return redirect(url_for("index"))

        if "clear_all" in request.form:
            session["files_data"] = []
            session["selected_column_index"] = None
            session.modified = True
            return redirect(url_for("index"))

        if "remove_file" in request.form:
            filename = request.form["remove_file"]
            session["files_data"] = [file for file in session["files_data"] if file["name"] != filename]
            file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            if os.path.exists(file_path):
                os.remove(file_path)
            session.modified = True
            return redirect(url_for("index"))

        if "select_column" in request.form:
            try:
                selected_column_index = int(request.form["column_index"])
                column_count = get_column_count(session["files_data"])
                if column_count is not None and 0 <= selected_column_index < column_count:
                    session["selected_column_index"] = selected_column_index
                else:
                    session["selected_column_index"] = None
            except ValueError:
                session["selected_column_index"] = None
            session.modified = True

    column_count = get_column_count(session["files_data"])
    same_columns = column_count is not None
    column_range = f"0 to {column_count - 1}" if same_columns else None

    return render_template("index.html", files_data=session["files_data"], same_columns=same_columns, column_range=column_range, selected_column_index=selected_column_index)

@app.route("/graph")
def graph():
    if "files_data" not in session or not session["files_data"]:
        return "<h2>No data available to plot. Please upload a file first.</h2>"

    selected_column_index = session.get("selected_column_index", None)
    if selected_column_index is None:
        return "<h2>Please select a valid column index.</h2>"

    dfs = []
    print("Files Data:", session["files_data"])
    for file_info in session["files_data"]:
        file_name = file_info["name"]
        print(f"Fetching data for file: {file_name}, column index: {selected_column_index}")
        column_data = get_column_data(file_name, selected_column_index)
        print(f"Retrieved column data: {column_data}")
        if column_data is None or not column_data:
            print(f"No valid column data for {file_name}")
            continue

        time_series = range(len(column_data))
        df = pd.DataFrame({"time": time_series, "data": column_data})
        # Filter out rows with negative values
        df = df[(df["data"] >= 0)]  # Changed to filter only "data" column
        if df.empty:
            print(f"No non-negative data for {file_name} in column {selected_column_index}")
            continue
        dfs.append((file_name, df))

    if not dfs:
        return "<h2>No valid data found to plot after filtering.</h2>"

    # Normalize time
    min_time = min(df["time"].min() for _, df in dfs)
    for _, df in dfs:
        df["time"] -= min_time

    # Create Bokeh figure
    p = figure(
        title="CSV Column Comparison",
        x_axis_label="Time (normalized)",
        y_axis_label="Data",
        width=1000,
        height=700,
        tools="pan,wheel_zoom,box_zoom,reset"
    )

    num_dfs = len(dfs)
    colors = Category10[10] if num_dfs <= 10 else viridis(num_dfs)

    for (name, df), color in zip(dfs, colors):
        source = ColumnDataSource(data={"x": df["time"].tolist(), "y": df["data"].tolist()})
        p.line("x", "y", source=source, legend_label=name, line_width=2, color=color)
        p.circle("x", "y", source=source, size=6, color=color, legend_label=name)

    hover = HoverTool(tooltips=[("Time", "@x"), ("Value", "@y")], mode="mouse")
    p.add_tools(hover, PanTool(), WheelZoomTool(), ResetTool())
    p.legend.click_policy = "hide"

    script, div = components(p)
    return render_template("graph.html", script=script, div=div, cdn_js=CDN.render())

if __name__ == "__main__":
    app.run(debug=True)