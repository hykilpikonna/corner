/* eslint-disable prefer-const */
/**
 * Regex used in markdown extension parsing
 */
const re = {
    command: /<!--{.*?}-->/g,
    hashes: /^#+/g,
}

declare global
{
    interface RegExp
    {
        find(s: string): RegExpExecArray | null;
    }
}

/**
 * Make sure that regex find is consistent
 * https://stackoverflow.com/questions/11477415/why-does-javascripts-regex-exec-not-always-return-the-same-value
 */
RegExp.prototype.find = function(s)
{
    const r = this.exec(s)
    this.lastIndex = 0
    return r
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
        const r = re.hashes.find(lines[i])
        if (!r) return -1
        const level = r[0].length

        // Find next same-level or higher-level header's index
        let j = i + 1
        for (; j < lines.length; j++)
        {
            const r = re.hashes.find(lines[j])
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
        i--
    }

    function hideLines(n: number)
    {
        lines.splice(i, n)
        i--
    }

    /**
     * Add something
     * @param s String
     */
    function add(s: string)
    {
        lines[i] = lines[i].replace(re.command, s)
    }

    /**
     * Collapse section
     */
    function collapseSection()
    {
        const e = findSectionEnd()
        const title = lines[i].substring(lines[i].indexOf(' ') + 1).replace(re.command, '')
        lines[i] = `<Collapse title="${encodeURIComponent(title)}">`
        lines.splice(e, 0, '</Collapse>\n')
    }

    // Run all commands in markdown
    while (i < lines.length)
    {
        console.log(`Line ${i}`)

        // Find commands
        const r = re.command.find(lines[i])
        if (r)
        {
            let cmd = r[0]
            cmd = cmd.substring(5, cmd.length - 5).trim()

            // Run cmd
            console.log(`Running command`, cmd)
            eval(cmd)
        }

        i++;
    }

    // If I don't call these functions somewhere, they will be deleted by vite build, and it will
    // raise an error when the functions are called in eval(). So I put function calls in an
    // impossible condition to prevent deletion.
    if (raw == 'STOP DELETING MY UNUSED FUNCTIONS!!!')
    {
        hideSection()
        hideLines(0)
        add('')
        collapseSection()
    }

    console.log(lines.join('\n'))
    return lines.join('\n')
}
