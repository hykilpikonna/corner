import * as THREE from 'three'
import * as helper from "@/animation/Helpers";

let renderer: THREE.WebGLRenderer, scene: THREE.Scene, camera: THREE.PerspectiveCamera
const objects = []

const config = {
    background: '#333',

    // Field of vision and cutoff frustum for near and far
    cam: {fov: 50, near: 1, far: 2000},
}

// ////////////////////
// Three.js scene code

function init(): void
{

}

function update(): void
{

}

// ///////////////////
// Three.js meta code

/**
 * Start the three.js rendering engine
 *
 * @param id: Canvas element id
 */
export function start(id: string): void
{
    scene = new THREE.Scene()
    scene.background = new THREE.Color(config.background)

    // Create camera
    camera = new THREE.PerspectiveCamera(config.cam.fov, window.innerWidth / window.innerHeight,
        config.cam.near, config.cam.far)
    camera.position.set(0, 0, 200)
    camera.lookAt(0, 0, 0)

    // @ts-ignore Create WebGL Renderer
    renderer = new THREE.WebGLRenderer({canvas: document.getElementById(id), antialias: true})
    onWindowResize()
    window.addEventListener('resize', onWindowResize)

    init()
    animate()
}

function onWindowResize()
{
    camera.aspect = window.innerWidth / window.innerHeight
    camera.updateProjectionMatrix()

    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setSize(window.innerWidth, window.innerHeight)
}

function animate(): void
{
    requestAnimationFrame(animate)
    update()
    renderer.render(scene, camera)
}
