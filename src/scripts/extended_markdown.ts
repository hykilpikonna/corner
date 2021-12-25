/* eslint-disable prefer-const */
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
export function parseExtensions(raw: string): string
{
    // @ts-ignore
    let lines = raw.replace('\r\n', '\n').split('\n')
    let i = 0

    /**
     * Find the end index of a section or -1
     */
    function findSectionEnd(): number
    {
        const r = re.hashes.exec(lines[i])
        if (!r) return -1
        const level = r[0].length

        // Find next same-level or higher-level header's index
        let j = i + 1
        for (; j < lines.length; j++)
        {
            const r = re.hashes.exec(lines[j])
            if (r && r[0].length <= level) break
        }
        return j
    }

    /**
     * Hide an entire hn section
     * Example:
     *
     * ### Header3 <!--{ hideSection() }-->
     *
     * ... Hidden items
     *
     * ### Next Header3
     */
    function hideSection()
    {
        const e = findSectionEnd()
        if (e != -1) lines.splice(i, e - i)
    }

    /**
     * Add something
     * @param s String
     */
    function add(s: string)
    {
        lines[i] = lines[i].replace(re.command, s)
    }

    // Run all commands in markdown
    while (i < lines.length)
    {
        console.log(`Line ${i}`)

        // Find commands
        const r = re.command.exec(lines[i])
        if (r)
        {
            let cmd = r[0]
            cmd = cmd.substring(5, cmd.length - 5).trim()

            // Run cmd
            console.log(`Running command`, cmd)
            eval(cmd)

            // Remove cmd
            lines[i] = lines[i].replace(re.command, '')
        }

        i++;
    }

    console.log(lines.join('\n'))
    return lines.join('\n')
}
