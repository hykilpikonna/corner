import {Mesh, Object3D} from "three";
import IUpdatable from "@/animation/components/IUpdatable";
import {circle} from "@/animation/Helpers";
import {camera, scene} from "@/animation/Home";
import {moused} from "@/animation/Trackers";

export default class Editor implements IUpdatable
{
    hand: Mesh
    radius = 10
    scale = 1

    constructor()
    {
        this.hand = circle('#664400', 0, 10)
        scene.add(this.hand)

        window.addEventListener('wheel', (e) =>
        {
            // e.preventDefault()
            console.log(e.deltaY)
            this.scale -= e.deltaY / 120 / 10
            this.hand.scale.set(this.scale, this.scale, this.scale)
        }, false)
    }

    update(dt: number): void
    {
        this.hand.position.copy(moused.pos)
        return
    }
}
