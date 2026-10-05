# ac.project.update  (requirements: requirement.project_management; operations: operation.project.update)
Feature: Update Project succeeds

  Scenario: Update Project succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When Change the Project details
    And Update Project
    Then The changes to the Project are saved
