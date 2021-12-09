import * as THREE from 'three'
import {colors, config} from "@/animation/Config";
import {DirectionalLight, HemisphereLight, Mesh} from "three";

export const vertexShader = `
    varying vec3 vWorldPosition;
    
    void main() {
        vec4 worldPosition = modelMatrix * vec4( position, 1.0 );
        vWorldPosition = worldPosition.xyz;
        
        gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
    }`;

export const fragmentShader = `
    uniform vec3 topColor;
    uniform vec3 bottomColor;
    uniform float offset;
    uniform float exponent;
    
    varying vec3 vWorldPosition;
    
    void main() {
        float h = normalize( vWorldPosition + offset ).y;
        gl_FragColor = vec4( mix( bottomColor, topColor, max( pow( max( h , 0.0), exponent ), 0.0 ) ), 1.0 );
    }`;

/**
 * Create sky dome
 * @param scene
 */
export function addSky(scene: THREE.Scene): Mesh
{
    const uniforms = {
        "topColor": { value: colors.sky.top },
        "bottomColor": { value: colors.sky.bottom },
        "offset": { value: 33 },
        "exponent": { value: 0.6 }
    }

    const skyGeo = new THREE.SphereGeometry( 4000, 32, 15 );
    const skyMat = new THREE.ShaderMaterial({
        uniforms: uniforms,
        vertexShader: vertexShader,
        fragmentShader: fragmentShader,
        side: THREE.BackSide
    });

    const sky = new THREE.Mesh(skyGeo, skyMat)
    scene.add(sky)
    scene.fog = new THREE.Fog(colors.sky.bottom, 1, 5000)

    return sky
}

/**
 * Create ground
 * @param scene
 */
export function addGround(scene: THREE.Scene): Mesh
{
    const groundGeo = new THREE.PlaneGeometry(10000, 10000)
    const groundMat = new THREE.MeshLambertMaterial({color: colors.sky.ground})
    const ground = new THREE.Mesh(groundGeo, groundMat)
    ground.position.y = -33
    ground.rotation.x = -Math.PI / 2
    ground.receiveShadow = true
    scene.add(ground)

    return ground
}

/**
 * Add hemisphere light
 * @param scene
 */
export function addHemiLight(scene: THREE.Scene): HemisphereLight
{
    const hemiLight = new THREE.HemisphereLight(colors.sky.top, colors.sky.ground, 0.6)
    hemiLight.position.set(0, 50, 0)
    scene.add(hemiLight)

    if (config.debug) scene.add(new THREE.HemisphereLightHelper(hemiLight, 10))

    return hemiLight
}

/**
 * Add directional light
 * @param scene
 */
export function addDirLight(scene: THREE.Scene): DirectionalLight
{
    // Directional light
    const dirLight = new THREE.DirectionalLight(colors.sky.dirLight, 1)
    dirLight.position.set(-1, 1.75, 1)
    dirLight.position.multiplyScalar(30)
    scene.add(dirLight)

    dirLight.castShadow = true

    dirLight.shadow.mapSize.width = 2048
    dirLight.shadow.mapSize.height = 2048

    const d = 50

    dirLight.shadow.camera.left = -d
    dirLight.shadow.camera.right = d
    dirLight.shadow.camera.top = d
    dirLight.shadow.camera.bottom = -d

    dirLight.shadow.camera.far = 3500
    dirLight.shadow.bias = -0.0001

    if (config.debug) scene.add(new THREE.DirectionalLightHelper(dirLight, 10))

    return dirLight
}
