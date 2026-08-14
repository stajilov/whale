# What is WHALE?

- Multiagent Harness
- With looping capabilities
- With Ontologies and Knolwedgebase 


## Glossary 

*Agent AI Harnesss* - runtime for multiagent systems with orchestrations based on LLLM backbone.
*Loop Engineering*  - a method to work with AI agents in a loop conrolled way. Following the pattern: act → observe → decide → repeat with exit criteria



## Specs
- exetensive memory, ideally it should have structured memory
- Should be able to launch swarms of agents
- Should have stop criteria
- It should be self-learning, inspired by hermes
- Should have tools available, as many as possible. Maybe: we can use dynamic creation, with a specialized subagent


### Worth mentioning
System Prompts
Tools, Skills, MCPs + and their descriptions
Bundled Infrastructure (filesystem, sandbox, browser)
Orchestration Logic (subagent spawning, handoffs, model routing)
Hooks/Middleware for deterministic execution (compaction, continuation, lint checks)



## Form
- CLI - bundles
- UI Too


## Dev Tools
- VS Code
- Python + Typer OR Go + Cobra
- github
- github actions for CI/CD




- opencode + Miniimax M3
- kiloode
- Codex
- Claude Code
and other


## Design

CLI Tool


`whale`  continues session form the pwd
Availble commands:

-h --help
-m --models / LLM backbone
-s --sessions
-sk --skills add nameoftheskill/SKILL.md
-ag --agents add [AGENT]


-cc --custom commands
--c -add  


## Config 
System folder + settings
json file (?)
~/.whale
skills
agents
-default
-custom
mcps
tool (loaded dynamically)
settings.json
memory -> mongo like, sql like (with a native tools to read from it)
sessions 




## Important section
- Cheap to run
- Vast access to file system and tools
- Should complete the tasks efficiently
- Should have feedback loop



## Acceptance criteria
- Reliable harness with memoery and self-evolving capabilies
- Lean in token consumption
- Guranteed result to execute tasks and deliver


## Misc
landing page
wiki docs
native integrations: memory engines like openwolf
curl and brew installation, apt etc installations
