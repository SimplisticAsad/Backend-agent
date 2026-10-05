# ac.project_member.browse  (requirements: requirement.project_assignment; operations: operation.project_member.list)
Feature: Browse Project Members succeeds

  Scenario: Browse Project Members succeeds
    Given the user is signed in as Manager
    When List Project Members
    Then The Project Member information is displayed
