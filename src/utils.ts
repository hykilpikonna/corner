import {Options, Vue} from "vue-class-component";
import {withKeys} from "vue";

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

    mounted(): void
    {
        document.addEventListener('keydown', this.keyListener)
    }

    unmounted(): void
    {
        document.removeEventListener('keydown', this.keyListener)
    }

    keyListener(e: KeyboardEvent): void
    {
        if (e.key in this.keybinds)
        {
            if (this.keybinds[e.key](e) !== false)
            {
                e.preventDefault()
            }
        }
    }
}
