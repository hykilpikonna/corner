import * as THREE from 'three'
import {Vector3} from 'three'
import {camera} from "@/animation/Home";

export let mouse: MouseEvent
export const moused = {x: 0, y: 0, pos: new Vector3()}

/**
 * Initialize mouse tracker
 */
export function initMouseTracker(): void
{
    document.onmousemove = (e: MouseEvent) =>
    {
        mouse = e
        moused.x = e.clientX / window.innerWidth * 2 - 1
        moused.y = -(e.clientY / window.innerHeight * 2 - 1)

        // Project to 3d position
        // https://www.reddit.com/r/threejs/comments/eba9l3/3d_cursor_using_threejs_html_css/
        // https://jsfiddle.net/atwfxdpd/10/
        const vector = new THREE.Vector3(moused.x, moused.y, 0.5)
        vector.unproject(camera)
        const dir = vector.sub(camera.position).normalize()
        const distance = -camera.position.z / dir.z
        moused.pos = camera.position.clone().add(dir.multiplyScalar(distance))
    }
}
