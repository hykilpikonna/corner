import * as THREE from 'three'
import * as helper from "@/animation/Helpers";
import {initMouseTracker, moused} from "@/animation/MouseTracker";
import {circle} from "@/animation/Helpers";

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
    const geometryBox = helper.box(50, 50, 50);

    const lineSegments = new THREE.LineSegments(geometryBox, new THREE.LineDashedMaterial({
        color: 0xffaa00,
        dashSize: 3,
        gapSize: 1
    }));
    lineSegments.computeLineDistances()

    objects.push(lineSegments)
    scene.add(lineSegments)

    scene.add(circle(0xffff00, 0, 5))
    scene.add(circle(0xff00ff, 1, 4))
    scene.add(circle(0x0000ff, 2, 3))
    scene.add(circle(0x00ffff, 3, 2))
    scene.add(circle(0xff0000, 4, 1))
}

function circle(color: number, z: number, r: number)
{
    const geometry = new THREE.CircleGeometry(r, 32)
    const material = new THREE.MeshBasicMaterial({color})
    const circle = new THREE.Mesh(geometry, material)
    circle.position.z = z
    return circle
}

function update(): void
{
    const time = Date.now() * 0.001;
    const obj = camera
    obj.rotation.y = THREE.MathUtils.lerp(obj.rotation.y, (mouse.x * Math.PI) / 10, 0.1)
    obj.rotation.x = THREE.MathUtils.lerp(obj.rotation.x, (mouse.y * Math.PI) / 10, 0.1)

    // scene.traverse((object) =>
    // {
    //     object.rotation.x = 0.25 * time;
    //     object.rotation.y = 0.25 * time;
    // });
    return
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
