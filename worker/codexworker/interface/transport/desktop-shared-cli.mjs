// Supported Desktop CLI override: JSONL frontend <-> documented Unix WebSocket.
// Never patches the application or calls its private app-tools pipe.
import {spawn} from "node:child_process";
import {readFile,stat} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {connectUnixWebSocket} from "./unix-websocket.mjs";

export function desktopServerOptions(argv) {
  const index = argv.indexOf("app-server");
  if (index < 0 || argv.includes("--help") || argv.includes("-h") || argv.includes("--print-startup-config") ||
      argv[index+1] && !argv[index+1].startsWith("-")) return null;
  const config = {};
  const args = [...argv.slice(0,index),...argv.slice(index+1)];
  for(let i=0;i<args.length;i++){
    const arg=args[i];
    if(arg==="--analytics-default-enabled" || arg==="--stdio") continue;
    if(arg==="--listen"){
      if(args[++i]!=="stdio://")throw Error("unsupported_shared_desktop_listener");
      continue;
    }
    let value;
    if(arg==="-c" || arg==="--config") value=args[++i];
    else if(arg.startsWith("--config=")) value=arg.slice(9);
    else throw Error("unsupported_shared_desktop_option");
    const at=value?.indexOf("=")??-1;
    if(at<1)throw Error("invalid_shared_desktop_config");
    const key=value.slice(0,at).trim();
    if(!/^[A-Za-z0-9_.@-]+$/.test(key))throw Error("invalid_shared_desktop_config_key");
    try{config[key]=JSON.parse(value.slice(at+1));}catch{throw Error("unsupported_shared_desktop_config_value");}
  }
  return {config};
}
export function desktopRequest(message, config) {
  if(!["thread/start","thread/resume","thread/fork"].includes(message?.method) || !Object.keys(config).length)return message;
  const original=message.params?.config;
  if(original!=null && (typeof original!=="object" || Array.isArray(original)))throw Error("invalid_native_config");
  return {...message,params:{...message.params,config:{...config,...original}}};
}
export async function loadDesktopConnection(env=process.env) {
  const home=env.CODEX_HOME||path.join(os.homedir(),".codex");
  const file=env.CODEXWORKER_SHARED_DESKTOP_CONFIG||path.join(home,"state/codexworker/desktop-shared-connection.json");
  const metadata=await stat(file);
  if(typeof process.getuid==="function" && metadata.uid!==process.getuid() || metadata.mode&0o077)
    throw Error("shared_desktop_config_not_private");
  const value=JSON.parse(await readFile(file,"utf8"));
  if(value.version!==1 || !path.isAbsolute(value.realCli??"") || !path.isAbsolute(value.socket??""))
    throw Error("invalid_shared_desktop_connection");
  return value;
}
export async function runDesktopServer(settings,options,{stdin=process.stdin,stdout=process.stdout,stderr=process.stderr}={}) {
  const channel=await connectUnixWebSocket(settings.socket);
  stderr.write("[shared-desktop] connected to shared Unix runtime\n");
  let buffer="",failed=false;
  channel.stdout.pipe(stdout,{end:false});
  const input=chunk=>{
    try{
      buffer+=chunk.toString("utf8");
      if(Buffer.byteLength(buffer)>16*1024*1024)throw Error("shared_desktop_input_too_large");
      let at;
      while((at=buffer.indexOf("\n"))>=0){
        const line=buffer.slice(0,at);buffer=buffer.slice(at+1);
        if(!line.trim())continue;
        let message;try{message=JSON.parse(line);}catch{channel.stdin.write(line+"\n");continue;}
        channel.stdin.write(JSON.stringify(desktopRequest(message,options.config))+"\n");
      }
    }catch{
      failed=true;stderr.write("[shared-desktop] input forwarding failed; no independent runtime was started\n");channel.kill();
    }
  };
  const stop=()=>channel.kill();
  stdin.on("data",input);stdin.once("end",stop);
  process.once("SIGTERM",stop);process.once("SIGINT",stop);
  channel.on("error",()=>{failed=true;stderr.write("[shared-desktop] shared connection closed unexpectedly\n");});
  return new Promise(resolve=>channel.once("exit",()=>{
    stdin.removeListener("data",input);stdin.removeListener("end",stop);stdin.pause();
    process.removeListener("SIGTERM",stop);process.removeListener("SIGINT",stop);resolve(failed?1:0);
  }));
}
async function main(){
  const settings=await loadDesktopConnection(),argv=process.argv.slice(2),options=desktopServerOptions(argv);
  if(options)return runDesktopServer(settings,options);
  // Version/auth/metadata commands retain the real CLI implementation.
  const env={...process.env};delete env.CODEX_CLI_PATH;
  const child=spawn(settings.realCli,argv,{stdio:"inherit",env});
  const stop=()=>child.kill("SIGTERM");process.once("SIGTERM",stop);
  return new Promise((resolve,reject)=>{
    child.once("error",reject);child.once("exit",code=>{process.removeListener("SIGTERM",stop);resolve(code??1);});
  });
}
if(process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  main().then(code=>{process.exitCode=code;},()=>{
    process.stderr.write("[shared-desktop] shared CLI could not start; no fallback engine was created\n");process.exitCode=1;
  });
}
