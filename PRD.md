# PRD: Black Relay 2026 Static Landing Page

## 1. Overview

Build a static website / landing page for **Black Relay 2026: Safehouse Systems**, an Arrowhead Research event centered on building a portable, installable, survivable safehouse operating stack for a temporary urban site.

The landing page should clearly explain the event concept, participant expectations, team structure, timeline, and expected outputs. It should make Black Relay feel like a serious technical systems-integration program rather than a generic hackathon, home-security project, or escape-room exercise.

## 2. Goals

- Explain what Black Relay 2026 is in plain, compelling language.
- Communicate the core challenge: rapidly emplacing a portable safehouse system that supports awareness, survivability, degraded operation, and operator decisions.
- Attract technically capable participants, advisors, and partners.
- Present the four main technical lanes / teams.
- Describe the four-month remote build rhythm and final in-person event.
- Provide clear calls to action for interested participants or collaborators.

## 3. Non-Goals

- Do not build a full event management system.
- Do not include participant login, registration workflows, payment, or account management unless added later.
- Do not over-explain sensitive operational details.
- Do not position the event as a home security product, training simulation, escape room, or standard CTF.

## 4. Target Audiences

### Primary Audience: Technical Participants

People with meaningful hands-on skill in one or more of:

- CAD / fabrication / hardware integration
- Sensors and detection systems
- Power, edge compute, networking, backhaul, infrastructure
- TAK, Android/Kotlin/Java, operator interfaces
- Fieldable hardware systems
- Containerization, CI/CD, deployment tooling
- RF, communications, or resilient systems

Participants should not need to be world-class experts, but they should be beyond beginner level. Coaching and mentorship are acceptable; starting from “hello world” is not.

### Secondary Audience: Technical Advisors and Partners

Industry, academic, government, or defense-adjacent collaborators who can provide mentorship, tools, hardware, review, or domain guidance.

### Tertiary Audience: Arrowhead Stakeholders

Internal and external stakeholders who need to understand what the event is, why it matters, and what success looks like.

## 5. Positioning

### Event Name

**Black Relay 2026: Safehouse Systems**

### Short Description

Black Relay 2026 is a four-month technical build culminating in an in-person field event where teams design, integrate, and deploy a portable safehouse operating stack for a temporary urban site.

### Core Message

Build a portable safehouse system that helps occupants understand what is happening, what is failing, and what to do next under degraded, adversarial, and time-constrained conditions.

### Tone

The site should feel:

- Serious
- Technical
- Field-oriented
- Operator-focused
- Mission-driven
- Selective but accessible
- Modern and credible

Preferred language:

- Fieldable
- Portable
- Survivable
- Operator-focused
- Systems integration
- Temporary urban site
- Degraded operation
- Rapid emplacement
- Awareness
- Interoperability
- Resilience

Avoid language that makes the event sound like:

- A home security project
- A generic hackathon
- A toy CTF
- An escape room
- A purely academic exercise

## 6. Core Problem Statement

How can a small team rapidly emplace a portable safehouse system in an unfamiliar site that provides awareness, detects degradation or tampering, maintains essential functions under disruption, and supports operator decisions during a multi-day stress period?

## 7. Site Structure

### 7.1 Hero Section

Purpose: Immediately explain the event and create interest.

Suggested copy:

> **Black Relay 2026: Safehouse Systems**  
> Build a portable, survivable safehouse operating stack for temporary urban sites.
>
> A four-month technical build culminating in an in-person field event focused on awareness, degraded operation, and operator decision support.

Primary CTA examples:

- Apply to Participate
- Express Interest
- Contact Arrowhead

Secondary CTA examples:

- View Technical Lanes
- Learn the Challenge

### 7.2 What It Is

Purpose: Explain the event model.

Content points:

- Black Relay 2026 is a technical systems-integration event.
- Participants work remotely over roughly four months.
- Teams build partially fieldable subsystems with documentation, interfaces, and test evidence.
- The program culminates in a live in-person integration and deployment phase.
- The final system should be deployable in an unfamiliar building with minimal setup and continue functioning when connectivity or individual components fail.

### 7.3 The Challenge

Purpose: Define the scenario and technical challenge.

Content points:

- Teams design a portable safehouse operating system for a temporary urban site.
- The system should provide local awareness, detect compromise or degradation, preserve essential functions, and support operator decision-making.
- The site may be unoccupied during part of the day.
- An adversary may attempt physical, RF, network, or sensor-level compromise.
- The system should assess its own survivability state, preserve and relay critical information offsite, and present useful context to remote decision-makers and returning occupants.

### 7.4 Technical Lanes / Teams

Purpose: Show how participants will be organized.

#### Fabrication Team

Goal: Make the system physically fieldable, modular, and maintainable.

Responsibilities:

- Collaborative CAD
- Enclosures
- Sensor mounts and brackets
- Cable routing
- Transportability
- Service access
- Physical integration standards
- Rapid setup and teardown workflows

Suggested outputs:

- System packaging concept
- Shared CAD
- Preliminary BOM
- Installation concept
- Physical integration standards
- Tamper-aware and serviceable hardware design

#### Detection Team

Goal: Turn raw sensor inputs into meaningful site awareness.

Responsibilities:

- Video, low-light, thermal, door/window, motion, acoustic, RF, and drone-relevant sensing concepts
- Sensor selection
- Ingest pipelines
- Time synchronization
- Confidence scoring
- Cross-sensor corroboration
- Alert tuning

Suggested outputs:

- Sensor architecture and coverage plan
- Event schema
- Fusion logic or confidence model
- Drone-relevant awareness concept
- Alert prioritization logic
- Test evidence

#### Continuity Team

Goal: Keep essential functions alive and visible under degraded conditions.

Responsibilities:

- Power model
- Battery / UPS behavior
- Local network resilience
- Edge compute placement
- Service health
- Offsite reporting
- Failover logic
- Tamper and degradation sensing
- Recovery and degraded-mode behavior

Suggested outputs:

- Network and compute architecture
- Power and backup concept
- Runtime assumptions
- Backhaul progression
- Health / heartbeat telemetry model
- Survivability and recovery logic
- Demonstrated failover behavior

#### TAK Team

Goal: Turn technical outputs into clear operator decisions.

Responsibilities:

- TAK plugin concept
- Operator workflows
- Common data model
- Alert and overlay taxonomy
- Health and degraded-state visualization
- Event normalization
- Thin supporting services as needed

Suggested outputs:

- TAK plugin concept
- Common ingest contract
- Alert hierarchy
- System health presentation
- Demo-ready plugin integrated with other teams’ data
- Test evidence showing decision support

### 7.5 Timeline

Purpose: Set expectations for the remote build and final event.

Suggested structure:

- **Month 1 — Requirements and Architecture**  
  Define subsystem concepts, architecture, interfaces, assumptions, and early integration points.

- **Month 2 — Prototyping**  
  Build early subsystem prototypes and validate technical feasibility.

- **Month 3 — Cross-Team Integration**  
  Connect subsystems, refine interfaces, discover failure modes, and iterate.

- **Month 4 — Hardening and Rehearsal**  
  Improve reliability, documentation, installation workflow, and final demo readiness.

- **In-Person Event — Deployment and Stress Testing**  
  Install, operate, and evaluate the integrated safehouse stack in a live setting.

### 7.6 Expected Outputs

Purpose: Make participation expectations concrete.

Each team should arrive at the in-person phase with more than a prototype. Expected outputs include:

- One-paragraph subsystem concept
- Architecture diagram
- Interface definition for what the subsystem consumes and publishes
- BOM or dependency list
- Installation or deployment notes
- Test plan
- Test evidence
- Risks and open issues
- Final in-person demo package

### 7.7 Who Should Apply

Purpose: Filter for appropriate participant fit.

Suggested copy:

> Black Relay is designed for builders who can contribute to a real technical system. You do not need to know everything on day one, but you should bring useful starting competence in at least one relevant domain. Coaching is available; starting from scratch is not the goal.

Relevant backgrounds:

- Military technologists
- Engineers and developers
- RF / communications practitioners
- Hardware builders
- CAD and fabrication contributors
- Cybersecurity and infrastructure specialists
- TAK / Android developers
- Edge compute and sensor systems builders

### 7.8 Success Criteria

Purpose: Explain what “good” looks like.

Success means participants produce a credible, modular, explainable, survivable, and operator-usable safehouse stack.

The strongest designs will:

- Install quickly in unfamiliar structures
- Continue functioning under degraded conditions
- Detect tampering or failure
- Preserve critical information
- Present useful decisions to operators
- Avoid unnecessary complexity
- Demonstrate clear interfaces between subsystems
- Include documentation and test evidence

### 7.9 CTA Section

Purpose: Convert interest into contact or application.

Possible CTA copy:

> Interested in building Black Relay 2026?  
> We are looking for technically capable participants, advisors, and partners who want to build fieldable systems under realistic constraints.

CTA buttons:

- Apply to Participate
- Become an Advisor
- Contact Arrowhead

## 8. Content Requirements

The landing page must include:

- Event name and subtitle
- Clear explanation of the safehouse systems concept
- Four technical lanes / teams
- Four-month build rhythm
- Final in-person event framing
- Participant expectations
- Expected outputs
- Strong CTA

The landing page should not include:

- Sensitive operational details
- Specific adversary tactics beyond high-level framing
- Private participant information
- Internal-only planning notes
- Unconfirmed dates, locations, or application deadlines unless supplied later

## 9. Design Requirements

The visual design should be:

- Static and fast-loading
- Mobile responsive
- Dark / tactical / technical without being cliché
- Clean and readable
- Strong typography
- Suitable for a serious technical audience

Potential visual motifs:

- Signal paths
- Modular systems
- Maps / grid overlays
- Low-light urban environment
- Hardware / enclosure / sensor abstractions
- Status indicators
- Network topology lines

Avoid excessive militarized or sci-fi aesthetics.

## 10. Functional Requirements

Minimum viable site:

- Single static landing page
- Responsive layout
- CTA links configurable via constants or content file
- Sections matching the content structure above
- Accessible semantic HTML
- Basic SEO metadata
- Open Graph metadata

Optional enhancements:

- Smooth scrolling navigation
- Static FAQ section
- Lightweight animation
- Team lane cards
- Timeline component
- Application link integration
- Downloadable one-page brief

## 11. Open Questions

- What is the official event date range for the final in-person phase?
- What location, if any, can be publicly listed?
- What should the primary CTA link to?
- Is there an existing application form?
- Should the site mention Arrowhead Research prominently in the hero or footer only?
- Are there approved logos, colors, or brand assets?
- Should Black Relay 2025 outcomes be referenced as credibility proof?
- Should the site include sponsor / advisor information?

## 12. Source Materials Reviewed

Primary source folder:

- Operations / Black Relay 2026

Relevant documents:

- Black Relay Planning Notes
- Initial Planning Doc
- Early Planning / Black Relay Concept: Safehouse Systems

Additional context reviewed:

- Operations / Black Relay 2025
- Prior Black Relay one-pager
- Prior Black Relay plan
- Prior final deployment plan
