"""Builds viewer.html: tray.stl + lid.stl inlined, an orbitable 3D view of the assembled box.
Run:  python make_viewer.py     Open viewer.html in any browser (needs internet once for three.js)."""
import base64
import os
import sys

SUF = sys.argv[1] if len(sys.argv) > 1 else ""

here = os.path.dirname(os.path.abspath(__file__))
b64 = lambda n: base64.b64encode(open(os.path.join(here, n), "rb").read()).decode()

html = """<!doctype html><meta charset="utf-8"><title>HomeGuard box viewer</title>
<style>
html,body{margin:0;height:100%;background:#1b1f24;color:#dde;font:14px Arial,sans-serif;overflow:hidden}
#ui{position:fixed;top:10px;left:10px;background:#0009;padding:10px 12px;border-radius:8px;line-height:2}
button{margin:0 2px}
#info{position:fixed;bottom:8px;left:10px;color:#9ab}
</style>
<div id="ui">
 Explode <input id="ex" type="range" min="0" max="80" value="0"><br>
 <label><input id="lidon" type="checkbox" checked> lid</label>
 <label><input id="ghost" type="checkbox"> see-through lid</label><br>
 View:
 <button data-v="front">front (lid)</button><button data-v="back">back (wall)</button>
 <button data-v="top">right side (vents)</button><button data-v="bottom">left side (type-C opening)</button><button data-v="iso">3/4</button>
</div>
<div id="info">Drag = rotate, wheel = zoom, right-drag = move. Box 115 x 90 x 55 mm; keypad window faces front.</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const TRAY="@@TRAY@@", LID="@@LID@@";
function geo(b64, map){
  const bin=atob(b64), u=new Uint8Array(bin.length); for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
  const dv=new DataView(u.buffer), n=dv.getUint32(80,true), p=new Float32Array(n*9);
  for(let t=0;t<n;t++){const o=84+t*50+12;for(let k=0;k<9;k++)p[t*9+k]=dv.getFloat32(o+k*4,true);}
  for(let i=0;i<p.length;i+=3){const v=map(p[i],p[i+1],p[i+2]);p[i]=v[0];p[i+1]=v[1];p[i+2]=v[2];}
  const g=new THREE.BufferGeometry(); g.setAttribute('position',new THREE.BufferAttribute(p,3)); g.computeVertexNormals(); return g;
}
// STL: X width, Y height, Z depth (back plate at z=0). Centre it on the origin; front faces +Z.
const cx=57.5, cy=45, cz=27.5;
const trayG=geo(TRAY,(x,y,z)=>[x-cx,y-cy,z-cz]);
const lidG=geo(LID,(x,y,z)=>[x-cx,y-cy,(55-z)-cz]);   // lid sits on the rim, outer face towards the viewer
const scene=new THREE.Scene(); scene.background=new THREE.Color(0x1b1f24);
const cam=new THREE.PerspectiveCamera(40,innerWidth/innerHeight,1,2000);
const r=new THREE.WebGLRenderer({antialias:true}); r.setSize(innerWidth,innerHeight); document.body.appendChild(r.domElement);
scene.add(new THREE.HemisphereLight(0xffffff,0x334455,0.9));
const d=new THREE.DirectionalLight(0xffffff,0.8); d.position.set(80,120,200); scene.add(d);
const mt=new THREE.MeshStandardMaterial({color:0xd9d9d4,roughness:0.6,flatShading:true,side:THREE.DoubleSide});
const ml=new THREE.MeshStandardMaterial({color:0x4aa3df,roughness:0.5,flatShading:true,side:THREE.DoubleSide,transparent:true,opacity:1});
const tray=new THREE.Mesh(trayG,mt), lid=new THREE.Mesh(lidG,ml); scene.add(tray,lid);
const ctl=new THREE.OrbitControls(cam,r.domElement); ctl.enableDamping=true;
const V={front:[0,0,330],back:[0,0,-330],top:[330,0,0.01],bottom:[-330,0,0.01],iso:[190,150,250]};
function view(k){cam.position.set(...V[k]);ctl.target.set(0,0,0);cam.up.set(0,1,0);ctl.update();}
view('iso');
document.querySelectorAll('button').forEach(b=>b.onclick=()=>view(b.dataset.v));
ex.oninput=()=>{lid.position.z=+ex.value};
lidon.onchange=()=>{lid.visible=lidon.checked};
ghost.onchange=()=>{ml.opacity=ghost.checked?0.3:1};
addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();r.setSize(innerWidth,innerHeight)});
(function loop(){requestAnimationFrame(loop);ctl.update();r.render(scene,cam)})();
</script>"""
open(os.path.join(here, f"viewer{SUF}.html"), "w", encoding="utf-8").write(
    html.replace("@@TRAY@@", b64(f"tray{SUF}.stl")).replace("@@LID@@", b64(f"lid{SUF}.stl")))
print(f"viewer{SUF}.html written")
