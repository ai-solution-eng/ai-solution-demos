from glob import glob

d = input("Which files? (S)cripts/(H)elm/(B)oth")
files: list[str | tuple[str, str]] = []

if d != "H":
    files.extend(glob("*.py"))
    files.extend(glob("main_components/*.py"))
    files.extend(glob("utils/*.py"))
    files.extend(glob("static/*.html"))

if d != "S":
    files.extend(glob("speechtospeech-chart/templates/*.yaml"))
    files.append("speechtospeech-chart/values.yaml")

query = ""

for file in files:
    if isinstance(file, tuple):
        filename, notes = file
        notes = " " + notes
    else:
        filename = file
        notes = ""
    try:
        with open(filename) as fid:
            data = fid.read()
            query += f"\n{filename}{notes}\n```\n{data}\n```"

    except Exception as e:
        print(f"For file {filename}, got error {e}. Skipping.")

query += "\n"

input("Ready?")
print(query)

with open("stringified_prompt.txt", "w") as fid:
    fid.write(query)
