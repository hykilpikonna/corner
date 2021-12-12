import {Options, Vue} from "vue-class-component";
import {withKeys} from "vue";
import {NodeLib} from "three/examples/jsm/nodes/core/NodeLib";
import keywords = NodeLib.keywords;

/**
 * Same as python's range
 *
 * Ways of calling this function:
 * range(to)
 * range(from, to)
 * range(from, to, step)
 *
 * @param fromOrTo Either from or to. From is inclusive
 * @param to to, Not inclusive
 * @param step Step
 */
export function range(fromOrTo: number, to?: number, step = 1): number[]
{
    const from = to !== undefined ? fromOrTo : 0
    to = to !== undefined ? to : fromOrTo

    if (to == from) return []
    const mul = to > from ? 1 : -1

    return [...Array(Math.floor((Math.abs(to - from)) / step))].map((_, i) => from + i * step * mul);
}

/**
 * Make sure that the value is between a min and a max
 *
 * @param val
 * @param min
 * @param max
 */
export function minMax(val: number, min: number, max: number): number
{
    return val > max ? max : val < min ? min: val
}

/**
 * Keybinds[key string] = (event) => Prevent default or not (Default: true)
 */
export type Keybinds = {[id: string]: (e: KeyboardEvent) => unknown}

/**
 * Key handler mixin
 */
export class KeyHandler extends Vue
{
    keybinds: Keybinds = {}
    _keybinds: Keybinds = {}

    initKeybinds(): void
    {
        return
    }

    mounted(): void
    {
        document.addEventListener('keydown', this.keyListener)
        this.initKeybinds()
        Object.keys(this.keybinds).forEach(it => this._keybinds[it.toLowerCase()] = this.keybinds[it])
    }

    unmounted(): void
    {
        document.removeEventListener('keydown', this.keyListener)
    }

    keyListener(e: KeyboardEvent): void
    {
        let key = e.key
        if (e.shiftKey) key = 'Shift' + key
        if (e.altKey) key = 'Alt' + key
        if (e.ctrlKey) key = 'Ctrl' + key
        if (e.metaKey) key = 'Cmd' + key
        console.log(key)

        if (key in this._keybinds)
        {
            if (this._keybinds[key](e) !== false)
            {
                e.preventDefault()
            }
        }
    }
}
