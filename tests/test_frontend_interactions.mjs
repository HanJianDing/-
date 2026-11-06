import test from 'node:test'
import assert from 'node:assert/strict'
import {handleComposerKeyDown, resetAccountNavigation, isNearLatest} from '../frontend/src/interaction.js'

function keyboard(changes={}, state={busy:false,text:'test'}) {
  let submitted=0, prevented=0
  handleComposerKeyDown({key:'Enter',preventDefault(){prevented++},
    currentTarget:{form:{requestSubmit(){submitted++}}},...changes},state)
  return {submitted,prevented}
}
test('Enter submits through the existing form',()=>assert.deepEqual(keyboard(),{submitted:1,prevented:1}))
for(const [name,change] of Object.entries({shift:{shiftKey:true},ctrl:{ctrlKey:true},alt:{altKey:true},meta:{metaKey:true},composition:{isComposing:true},nativeComposition:{nativeEvent:{isComposing:true}},ime229:{keyCode:229},native229:{nativeEvent:{keyCode:229}},otherKey:{key:'a'}})) {
  test(`${name} does not submit or suppress native input`,()=>assert.deepEqual(keyboard(change),{submitted:0,prevented:0}))
}
test('held Enter cannot submit repeatedly',()=>assert.deepEqual(keyboard({repeat:true}),{submitted:0,prevented:1}))
test('busy form cannot submit',()=>assert.equal(keyboard({}, {busy:true,text:'test'}).submitted,0))
test('blank form cannot submit',()=>assert.equal(keyboard({}, {busy:false,text:' \n '}).submitted,0))
test('account reset replaces stale URL and clears pending task question',()=>{
  const calls=[]
  resetAccountNavigation({location:{pathname:'/app',search:'?view=1'},history:{replaceState(...args){calls.push(args)}},setQuestion(x){calls.push(['question',x])},setRoute(x){calls.push(['route',x])}})
  assert.deepEqual(calls,[[null,'','/app?view=1#dashboard'],['question',''],['route','dashboard']])
})
test('reading older messages disables follow-latest',()=>assert.equal(isNearLatest({scrollHeight:3000,clientHeight:500,scrollTop:100}),false))
test('returning to bottom permits follow-latest',()=>assert.equal(isNearLatest({scrollHeight:3000,clientHeight:500,scrollTop:2490}),true))
