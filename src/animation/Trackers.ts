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
        moused.pos = projectTo3D(moused.x, moused.y, 0)
    }
}

/**
 * Project to 3D position
 * https://www.reddit.com/r/threejs/comments/eba9l3/3d_cursor_using_threejs_html_css/
 * https://jsfiddle.net/atwfxdpd/10/
 *
 * @param screenX X position on 2D canvas
 * @param screenY Y position on 2D canvas
 * @param z Z position in 3D
 */
export function projectTo3D(screenX: number, screenY: number, z: number): Vector3
{
    const vector = new THREE.Vector3(screenX, screenY, 0.5)
    vector.unproject(camera)
    const dir = vector.sub(camera.position).normalize()
    const distance = (-camera.position.z + z) / dir.z
    const pos = camera.position.clone().add(dir.multiplyScalar(distance))
    // console.log('Dir:', dir)
    // console.log('Pos:', pos)
    return pos
}
