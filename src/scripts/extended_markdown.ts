/**
 * Regex used in markdown extension parsing
 */
const re = {
    command: /<!--{.*?}-->/g,
    hashes: /^#+/g,
}

/**
 * Parse markdown extensions
 *
 * @param raw Extended markdown
 * @return Parsed markdown
 */
function parseExtensions(raw: string): string
{
    const lines = raw.replace('\r\n', '\n').split('\n')
    let i = 0

    return raw
}
