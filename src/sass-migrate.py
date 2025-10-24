from pathlib import Path
import re
import subprocess
import tempfile

def extract_and_migrate_sass(file_path):
    """Extracts <style lang="sass"> block from a Svelte file, runs sass-migrator on it, and replaces the original style block."""

    with open(file_path, 'r', encoding='utf-8') as file:
        content = file.read()

    # Regex pattern to match <style lang="sass">...</style>
    style_pattern = re.compile(r'<style\s+lang\s*=\s*["\']sass["\']( scoped)*>(.*?)</style>', re.DOTALL)

    # Extract all matching style blocks
    matches = list(style_pattern.finditer(content))

    if not matches:
        print(f"No <style lang='sass'> block found in {file_path}")
        return

    updated_content = content

    for match in matches:
        original_style_block = match.group(0)  # The full <style>...</style> block
        scoped = match.group(1)  # The scoped attribute if present
        sass_content = match.group(2)  # The content inside the <style> block

        # Create a temporary file in the same directory as the .svelte file
        temp_file_path = Path(file_path).parent / f"{Path(file_path).stem}_temp.sass"

        # Write the SASS content to the temporary file
        with open(temp_file_path, 'w', encoding='utf-8') as temp_file:
            # Calculate relative path depth
            depth = len(Path(file_path).parent.relative_to(Path(__file__).parent).parts)
            print(Path(file_path).parent.relative_to(Path(__file__).parent).parts)
            temp_file.write(sass_content.replace("src/", "../" * depth))

        # Run the sass-migrator on the temporary file
        try:
            subprocess.run(['sass-migrator', 'module', str(temp_file_path)], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error while running sass-migrator on {file_path}: {e}")
            continue

        # Read back the migrated content
        with open(temp_file_path, 'r', encoding='utf-8') as temp_file:
            migrated_sass_content = temp_file.read()

        # Create the new <style> block with the migrated SASS content
        new_style_block = f'<style lang="sass"{scoped or ''}>{migrated_sass_content}</style>'

        # Replace the original style block with the new one
        updated_content = updated_content.replace(original_style_block, new_style_block)

        # Remove the temporary file
        temp_file_path.unlink()

    # Write the updated content back to the original file
    with open(file_path, 'w', encoding='utf-8') as file:
        file.write(updated_content)

    print(f"Updated {file_path}")

def process_svelte_files(directory):
    """Recursively processes all .svelte files in the given directory."""
    svelte_files = Path(directory).rglob("*.vue")

    for svelte_file in svelte_files:
        extract_and_migrate_sass(svelte_file)

def main():
    """Main function to process all .svelte files in the current directory."""
    current_directory = Path(__file__).parent
    process_svelte_files(current_directory)

if __name__ == "__main__":
    main()